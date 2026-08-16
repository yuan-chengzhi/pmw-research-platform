"""Deterministic, snapshot-bound briefing for a mathematical research cohort."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Callable, Mapping

from .legacy_frontier import build_legacy_frontier_view
from .records import RESEARCH_RECORD_SCHEMA, canonical_json
from .store import ResearchWorld, ResearchWorldError, WorldAdmission


SITUATION_SCHEMA = "PMW_MATHEMATICAL_SITUATION_1"
MAXIMUM_SITUATION_BYTES = 16 * 1024 * 1024
MAXIMUM_PROJECTED_STRING_BYTES = 12_000
MAXIMUM_PROJECTED_ITEMS = 128
_ARTIFACT_REF = re.compile(r"^artifact/sha256/[0-9a-f]{64}$")
_WORLD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _truncate_text(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= MAXIMUM_PROJECTED_STRING_BYTES:
        return value
    suffix = b"...[projection truncated; fetch exact admission]"
    return (
        encoded[: MAXIMUM_PROJECTED_STRING_BYTES - len(suffix)] + suffix
    ).decode("utf-8", errors="ignore")


def _compact(value: object, *, depth: int = 0) -> object:
    """Keep a useful bounded projection while making every omission explicit."""

    if depth > 8:
        return "[projection depth limit; fetch exact admission]"
    if type(value) is str:
        return _truncate_text(value)
    if type(value) in {int, float, bool} or value is None:
        return value
    if type(value) is list:
        selected = [
            _compact(item, depth=depth + 1)
            for item in value[:MAXIMUM_PROJECTED_ITEMS]
        ]
        if len(value) > len(selected):
            selected.append({"omitted_items": len(value) - len(selected)})
        return selected
    if type(value) is dict:
        keys = sorted(value)[:MAXIMUM_PROJECTED_ITEMS]
        selected = {
            str(key): _compact(value[key], depth=depth + 1) for key in keys
        }
        if len(value) > len(keys):
            selected["projection_omitted_keys"] = len(value) - len(keys)
        return selected
    return f"[unsupported projected value: {type(value).__name__}]"


def _artifact_refs(value: object) -> tuple[str, ...]:
    selected: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if type(item) is str and _ARTIFACT_REF.fullmatch(item) is not None:
            selected.add(item)
        elif type(item) is list:
            stack.extend(item)
        elif type(item) is dict:
            stack.extend(item.values())
    return tuple(sorted(selected))


_MATHEMATICAL_FIELDS = (
    "kind",
    "update_kind",
    "title",
    "summary",
    "body",
    "claim_ceiling",
    "evidence_status",
    "next_needs",
    "decision",
    "decisive_unknown",
    "route",
    "stop_or_pivot_condition",
    "statement",
    "claim_kind",
    "submission_kind",
    "authority_effect",
    "verification_scope",
    "verifier_result",
    "life_id",
    "campaign_id",
)


def _project_content(content: object) -> dict[str, object]:
    if type(content) is not dict:
        return {}
    provenance: dict[str, object] = {}
    selected: Mapping[str, object] = content
    wrapper_omitted: tuple[str, ...] = ()
    if content.get("schema") == "PMW_FRONTIER_PREDECESSOR_RECORD_1":
        original = content.get("original")
        if type(original) is dict:
            selected = original
            provenance = {
                "provenance": "PREDECESSOR_IMPORT",
                "predecessor_admission_ref": content.get(
                    "predecessor_admission_ref"
                ),
                "predecessor_campaign_id": content.get(
                    "predecessor_campaign_id"
                ),
                "evidence_ceiling": content.get("evidence_ceiling"),
            }
            wrapper_omitted = tuple(sorted(
                set(content)
                - {
                    "schema",
                    "original",
                    "predecessor_admission_ref",
                    "predecessor_campaign_id",
                    "evidence_ceiling",
                }
            ))
    projection: dict[str, object] = dict(provenance)
    projection["content_schema"] = selected.get("schema", content.get("schema"))
    represented = {"schema", "artifact_refs"}
    for key in _MATHEMATICAL_FIELDS:
        if key in selected:
            projection[key] = _compact(selected[key])
            represented.add(key)
    if selected.get("schema") == RESEARCH_RECORD_SCHEMA:
        projection["problem_ids"] = _compact(selected.get("problem_ids", []))
        projection["payload"] = _compact(selected.get("payload", {}))
        represented.update({"problem_ids", "payload"})
    receipt = selected.get("artifact_receipt")
    if type(receipt) is dict:
        projection["artifact_receipt"] = _compact({
            key: receipt[key]
            for key in (
                "artifact_ref",
                "bytes",
                "claim_kind",
                "receipt_ref",
                "statement",
                "submission_kind",
            )
            if key in receipt
        })
        represented.add("artifact_receipt")
    omitted = sorted(set(selected) - represented)
    if omitted:
        projection["omitted_projected_content_fields"] = omitted
    if wrapper_omitted:
        projection["omitted_predecessor_wrapper_fields"] = list(wrapper_omitted)
    return projection


def _project_row(
    row: WorldAdmission,
    *,
    artifact_exists: Callable[[str], bool] | None,
) -> dict[str, object]:
    projection = _project_content(row.content)
    refs = _artifact_refs(row.content)
    if artifact_exists is None:
        availability: object = "NOT_CHECKED"
    else:
        availability = {
            ref: "AVAILABLE" if artifact_exists(ref) else "MISSING" for ref in refs
        }
    return {
        "admission_ref": row.admission_ref,
        "parent_refs": list(row.parent_refs),
        "stored_schema": row.schema,
        "content_sha256": hashlib.sha256(row.content_bytes).hexdigest(),
        "content_bytes": len(row.content_bytes),
        "artifact_refs": list(refs),
        "artifact_availability": availability,
        "mathematical_projection": projection,
        "exact_content": {
            "method": "world.get",
            "admission_ref": row.admission_ref,
        },
    }


@dataclass(frozen=True, slots=True)
class MathematicalSituation:
    """Detached canonical briefing bytes and their launch-binding digest."""

    world_id: str
    world_ref: str
    snapshot_ref: str
    sha256: str
    _bytes: bytes = field(repr=False)

    @property
    def bytes(self) -> bytes:
        return self._bytes

    @property
    def value(self) -> dict[str, object]:
        value = json.loads(self._bytes.decode("utf-8"))
        if type(value) is not dict:
            raise AssertionError("situation root is not an object")
        return value


def build_mathematical_situation(
    world: ResearchWorld,
    *,
    world_id: str,
    snapshot_ref: str | None = None,
    artifact_exists: Callable[[str], bool] | None = None,
) -> MathematicalSituation:
    """Build the exact problem set plus a loss-aware index of current state.

    Target cards are included in full.  Every other admission is represented
    once by a bounded mathematical projection, content digest, and exact
    retrieval reference.  This avoids silently dropping history while keeping
    the initial briefing suitable for a model context.
    """

    if (
        type(world_id) is not str
        or _WORLD_ID.fullmatch(world_id) is None
        or (world.world_id is not None and world.world_id != world_id)
    ):
        raise ResearchWorldError("SITUATION_WORLD_MISMATCH")
    selected_snapshot = world.head() if snapshot_ref is None else snapshot_ref
    rows = world.records(selected_snapshot)
    by_ref = {row.admission_ref: row for row in rows}
    legacy = build_legacy_frontier_view(world, snapshot_ref=selected_snapshot)
    problem_entries: list[dict[str, object]] = []
    scoped_refs: set[str] = set()
    for problem in legacy.problems:
        scoped_refs.update(problem.research_admission_refs)
        problem_entries.append({
            "problem_id": problem.problem_id,
            "target_card_admission_ref": problem.target_card_admission_ref,
            "target_card_ref": problem.target_card_ref,
            "problem": problem.card,
            "research_admission_refs": list(problem.research_admission_refs),
        })
    target_card_refs = {
        problem.target_card_admission_ref for problem in legacy.problems
    }
    unscoped_refs = sorted(set(by_ref) - scoped_refs - target_card_refs)
    value: dict[str, object] = {
        "schema": SITUATION_SCHEMA,
        "world_id": world_id,
        "world_ref": world.world_ref,
        "snapshot_ref": selected_snapshot,
        "problem_count": len(problem_entries),
        "admission_count": len(rows),
        "problems": problem_entries,
        "records": [
            _project_row(by_ref[reference], artifact_exists=artifact_exists)
            for reference in sorted(set(by_ref) - target_card_refs)
        ],
        "unscoped_admission_refs": unscoped_refs,
        "schema_counts": legacy.schema_counts,
        "semantics": {
            "problem_cards": "EXACT_CONTENT_AT_SNAPSHOT",
            "records": "ONE_BOUNDED_LOSS_AWARE_PROJECTION_PER_NON_CARD_ADMISSION",
            "problem_links": "RESEARCH_ADMISSION_REFS_JOIN_TO_RECORDS",
            "truth_ranking": "NONE",
            "exact_record_access": "world.get(admission_ref, snapshot_ref)",
        },
    }
    try:
        raw = canonical_json(value) + b"\n"
    except Exception as error:
        raise ResearchWorldError("SITUATION_ENCODING_FAILED") from error
    if len(raw) > MAXIMUM_SITUATION_BYTES:
        raise ResearchWorldError(
            "SITUATION_TOO_LARGE",
            "publish a reviewed CHECKPOINT summary before planning more sessions",
        )
    digest = hashlib.sha256(raw).hexdigest()
    return MathematicalSituation(
        world_id=world_id,
        world_ref=world.world_ref,
        snapshot_ref=selected_snapshot,
        sha256=digest,
        _bytes=raw,
    )
