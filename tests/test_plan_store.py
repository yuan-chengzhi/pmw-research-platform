from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pmw_platform.sessions import (
    CohortPlan,
    PlanStoreError,
    load_plan,
    plan_sha256,
    save_plan,
)


SNAPSHOT = "snapshot/sha256/" + "f" * 64
BRIEFING = (
    json.dumps(
        {
            "schema": "PMW_MATHEMATICAL_SITUATION_1",
            "world_id": "math-frontier",
            "world_ref": "refs/pmw/math-frontier",
            "snapshot_ref": SNAPSHOT,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    + "\n"
).encode()


def _plan(briefing: bytes = BRIEFING) -> CohortPlan:
    return CohortPlan.generate(
        cohort_id="cohort-clean",
        world_id="math-frontier",
        world_ref="refs/pmw/math-frontier",
        base_snapshot_ref=SNAPSHOT,
        safety_profile="research-default",
        safety_profile_sha256="1" * 64,
        core_lock_sha256="2" * 64,
        briefing_sha256=hashlib.sha256(briefing).hexdigest(),
        count=4,
        concurrency=2,
    )


def test_plan_store_roundtrip_and_refuses_identity_reuse(tmp_path: Path) -> None:
    plan = _plan()
    path = save_plan(plan, tmp_path, briefing_bytes=BRIEFING)

    assert path == tmp_path / "runs" / "cohort-clean" / "plan.json"
    assert load_plan(path) == plan
    assert len(plan_sha256(plan)) == 64
    assert "count" not in json.loads(path.read_text())
    with pytest.raises(PlanStoreError, match="already exists"):
        save_plan(plan, tmp_path, briefing_bytes=BRIEFING)


def test_plan_loader_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text('{"schema":"x","schema":"y"}')
    with pytest.raises(PlanStoreError, match="duplicate JSON key"):
        load_plan(path)


def test_plan_store_rejects_runs_symlink_escape(tmp_path: Path) -> None:
    data = tmp_path / "data"
    outside = tmp_path / "outside"
    data.mkdir()
    outside.mkdir()
    (data / "runs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PlanStoreError, match="must not be a symlink"):
        save_plan(_plan(), data, briefing_bytes=BRIEFING)
    assert not any(outside.iterdir())


def test_plan_store_rejects_a_briefing_with_an_unbound_identity(
    tmp_path: Path,
) -> None:
    wrong = BRIEFING.replace(b"math-frontier", b"other-world", 1)
    plan = _plan(wrong)
    with pytest.raises(PlanStoreError, match="identity does not match"):
        save_plan(plan, tmp_path, briefing_bytes=wrong)


def test_concurrent_identity_reuse_leaves_one_complete_bundle(
    tmp_path: Path,
) -> None:
    plan = _plan()

    def attempt() -> object:
        try:
            return save_plan(plan, tmp_path, briefing_bytes=BRIEFING)
        except PlanStoreError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: attempt(), range(2)))

    assert sum(isinstance(value, Path) for value in outcomes) == 1
    assert sum(isinstance(value, PlanStoreError) for value in outcomes) == 1
    path = tmp_path / "runs" / "cohort-clean" / "plan.json"
    assert load_plan(path) == plan
    assert not any(
        row.name.startswith(".cohort-clean.")
        for row in (tmp_path / "runs").iterdir()
    )
