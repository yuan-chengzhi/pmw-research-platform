from __future__ import annotations

import json

import pytest

from pmw_platform.world import ResearchRecord, ResearchRecordError


SNAPSHOT = "snapshot/sha256/" + "1" * 64
PARENT_A = "admission/sha256/" + "a" * 64
PARENT_B = "admission/sha256/" + "b" * 64
ARTIFACT = "artifact/sha256/" + "c" * 64


def make_record(**overrides: object) -> ResearchRecord:
    values: dict[str, object] = {
        "world_id": "math-frontier",
        "cohort_id": "cohort-001",
        "session_id": "session-001",
        "base_snapshot_ref": SNAPSHOT,
        "kind": "NOTE",
        "problem_ids": ("erdos-64", "degree-diameter-3-9-record"),
        "parent_refs": (PARENT_B, PARENT_A),
        "artifact_refs": (ARTIFACT,),
        "title": "A reusable reduction",
        "body": "The reduction preserves the exact finite predicate.",
        "payload": {"z": [3, 2, 1], "a": {"checked": True}},
    }
    values.update(overrides)
    return ResearchRecord(**values)  # type: ignore[arg-type]


def test_research_record_is_canonical_and_detached() -> None:
    first = make_record()
    second = make_record(
        problem_ids=("degree-diameter-3-9-record", "erdos-64"),
        parent_refs=(PARENT_A, PARENT_B),
        payload={"a": {"checked": True}, "z": [3, 2, 1]},
    )
    assert first.to_bytes() == second.to_bytes()
    assert first.problem_ids == tuple(sorted(first.problem_ids))
    assert first.parent_refs == (PARENT_A, PARENT_B)
    assert ResearchRecord.from_bytes(first.to_bytes()) == first
    assert len(first.content_sha256) == 64

    detached = first.payload
    detached["new"] = "not durable"
    assert "new" not in first.payload
    assert first.to_bytes() == second.to_bytes()


def test_research_record_rejects_noncanonical_or_ambiguous_json() -> None:
    record = make_record()
    value = record.to_value()
    noncanonical = json.dumps(value, ensure_ascii=False, sort_keys=False).encode()
    with pytest.raises(ResearchRecordError) as caught:
        ResearchRecord.from_bytes(noncanonical)
    assert caught.value.code == "NONCANONICAL_JSON"

    duplicate = record.to_bytes().replace(
        b'{"artifact_refs"', b'{"artifact_refs":[],"artifact_refs"', 1
    )
    with pytest.raises(ResearchRecordError) as caught:
        ResearchRecord.from_bytes(duplicate)
    assert caught.value.code == "MALFORMED_JSON"

    reordered = record.to_value()
    reordered["problem_ids"] = list(reversed(reordered["problem_ids"]))
    raw = json.dumps(
        reordered,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    with pytest.raises(ResearchRecordError) as caught:
        ResearchRecord.from_bytes(raw)
    assert caught.value.code == "NONCANONICAL_RECORD"


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"kind": "CLAIMED_SOLVED"}, "MALFORMED_FIELD"),
        ({"payload": {"estimate": 0.25}}, "PAYLOAD_TYPE_UNSUPPORTED"),
        ({"parent_refs": (PARENT_A, PARENT_A)}, "MALFORMED_FIELD"),
        ({"body": "x" * 48_001}, "FIELD_LIMIT_EXCEEDED"),
        ({"base_snapshot_ref": "head"}, "MALFORMED_FIELD"),
    ],
)
def test_research_record_rejects_invalid_fields(
    override: dict[str, object], code: str
) -> None:
    with pytest.raises(ResearchRecordError) as caught:
        make_record(**override)
    assert caught.value.code == code
