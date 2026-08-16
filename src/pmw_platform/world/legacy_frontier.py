"""Read-only materialization of the settled frontier-choice world.

This adapter recognizes historical record shapes so the M03 mathematical
state can be read as one problem-oriented view.  It never rewrites, wraps, or
admits legacy evidence.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import re

from .records import RESEARCH_RECORD_SCHEMA, canonical_json
from .store import ResearchWorld, ResearchWorldError, WorldAdmission


LEGACY_TARGET_CARD_SCHEMA = "PMW_FRONTIER_TARGET_CARD_1"
_PROBLEM_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_TARGET_CARD_REF = re.compile(r"^target-card/sha256/[0-9a-f]{64}$")


@dataclass(frozen=True)
class LegacyProblemView:
    """One legacy problem definition plus every scoped record at a snapshot."""

    problem_id: str
    target_card_admission_ref: str
    target_card_ref: str
    research_admission_refs: tuple[str, ...]
    _card_bytes: bytes = field(repr=False)

    @property
    def card(self) -> dict[str, object]:
        value = json.loads(self._card_bytes.decode("utf-8"))
        if type(value) is not dict:
            raise AssertionError("legacy target card is not an object")
        return value

    def to_value(self) -> dict[str, object]:
        return {
            "problem_id": self.problem_id,
            "target_card_admission_ref": self.target_card_admission_ref,
            "target_card_ref": self.target_card_ref,
            "card": self.card,
            "research_admission_refs": list(self.research_admission_refs),
        }


@dataclass(frozen=True)
class LegacyFrontierView:
    """Detached snapshot view; it intentionally has no mutation methods."""

    snapshot_ref: str
    problems: tuple[LegacyProblemView, ...]
    unscoped_admission_refs: tuple[str, ...]
    _schema_counts: tuple[tuple[str, int], ...]

    @property
    def schema_counts(self) -> dict[str, int]:
        return dict(self._schema_counts)

    def problem(self, problem_id: str) -> LegacyProblemView:
        for item in self.problems:
            if item.problem_id == problem_id:
                return item
        raise KeyError(problem_id)

    def to_value(self) -> dict[str, object]:
        return {
            "schema": "PMW_LEGACY_FRONTIER_VIEW_1",
            "snapshot_ref": self.snapshot_ref,
            "problems": [problem.to_value() for problem in self.problems],
            "unscoped_admission_refs": list(self.unscoped_admission_refs),
            "schema_counts": self.schema_counts,
            "semantics": (
                "READ_ONLY_GROUPING_OF_EXACT_PMW_RECORDS_NO_TRUTH_RANKING"
            ),
        }


def _content_targets(row: WorldAdmission) -> tuple[str, ...]:
    content = row.content
    if type(content) is not dict:
        return ()
    if content.get("schema") == RESEARCH_RECORD_SCHEMA:
        values = content.get("problem_ids")
        if type(values) is not list:
            return ()
        selected = tuple(
            value
            for value in values
            if type(value) is str and _PROBLEM_ID.fullmatch(value) is not None
        )
        return tuple(sorted(set(selected)))
    for field_name in ("target_id", "predecessor_target_id"):
        value = content.get(field_name)
        if type(value) is str and _PROBLEM_ID.fullmatch(value) is not None:
            return (value,)
    return ()


def _target_card(row: WorldAdmission) -> tuple[str, str, dict[str, object]] | None:
    content = row.content
    if (
        type(content) is not dict
        or content.get("schema") != LEGACY_TARGET_CARD_SCHEMA
    ):
        return None
    problem_id = content.get("target_id")
    card = content.get("card")
    target_card_sha256 = content.get("target_card_sha256")
    if (
        type(problem_id) is not str
        or _PROBLEM_ID.fullmatch(problem_id) is None
        or type(card) is not dict
        or type(target_card_sha256) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", target_card_sha256)
    ):
        raise ResearchWorldError(
            "LEGACY_FRONTIER_VIEW_INVALID", "target card fields"
        )
    target_card_ref = card.get("target_card_ref")
    if target_card_ref is None:
        target_card_ref = f"target-card/sha256/{target_card_sha256}"
    if (
        type(target_card_ref) is not str
        or _TARGET_CARD_REF.fullmatch(target_card_ref) is None
        or target_card_ref != f"target-card/sha256/{target_card_sha256}"
    ):
        raise ResearchWorldError(
            "LEGACY_FRONTIER_VIEW_INVALID", "target card identity"
        )
    return problem_id, target_card_ref, card


def build_legacy_frontier_view(
    world: ResearchWorld,
    *,
    snapshot_ref: str | None = None,
) -> LegacyFrontierView:
    """Group an exact legacy snapshot by problem without changing the world."""

    if not isinstance(world, ResearchWorld):
        raise TypeError("world must be ResearchWorld")
    selected_snapshot = world.head() if snapshot_ref is None else snapshot_ref
    rows = world.records(selected_snapshot)
    cards: dict[str, tuple[str, str, dict[str, object]]] = {}
    schema_counts: Counter[str] = Counter()
    for row in rows:
        schema_counts[row.schema or "NON_JSON_OR_UNTYPED"] += 1
        selected = _target_card(row)
        if selected is None:
            continue
        problem_id, target_card_ref, card = selected
        if problem_id in cards:
            raise ResearchWorldError(
                "LEGACY_FRONTIER_VIEW_INVALID", "duplicate target card"
            )
        cards[problem_id] = (row.admission_ref, target_card_ref, card)
    if not cards:
        raise ResearchWorldError(
            "LEGACY_FRONTIER_VIEW_INVALID", "no target cards"
        )

    related: dict[str, set[str]] = {problem_id: set() for problem_id in cards}
    unscoped: set[str] = set()
    card_refs = {value[0] for value in cards.values()}
    for row in rows:
        if row.admission_ref in card_refs:
            continue
        targets = tuple(
            target for target in _content_targets(row) if target in cards
        )
        if not targets:
            unscoped.add(row.admission_ref)
            continue
        for target in targets:
            related[target].add(row.admission_ref)

    problems = tuple(
        LegacyProblemView(
            problem_id=problem_id,
            target_card_admission_ref=cards[problem_id][0],
            target_card_ref=cards[problem_id][1],
            research_admission_refs=tuple(sorted(related[problem_id])),
            _card_bytes=canonical_json(cards[problem_id][2]),
        )
        for problem_id in sorted(cards)
    )
    return LegacyFrontierView(
        snapshot_ref=selected_snapshot,
        problems=problems,
        unscoped_admission_refs=tuple(sorted(unscoped)),
        _schema_counts=tuple(sorted(schema_counts.items())),
    )
