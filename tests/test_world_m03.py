from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from pmw_platform.sessions import CohortPlan
from pmw_platform.world import (
    PmwWriterAuthority,
    ResearchContribution,
    ResearchWorld,
    build_legacy_frontier_view,
    build_mathematical_situation,
)


M03_SNAPSHOT = (
    "snapshot/sha256/"
    "803bfcd0604ff01c9b560d9e82d6e2a9f606d6c36312418f2e95cf1193f3535a"
)
M03_WORLD_REF = "refs/pmw/frontier-choice-world"
M03_POLICY_FINGERPRINT = (
    "8507f7f7ea6f35b90183eaf1e27c82a578dd03f8ceec3dad8d61a2696ab6010d"
)


def _source_world() -> Path:
    value = os.environ.get("PMW_M03_WORLD_REPO")
    if not value:
        pytest.skip("PMW_M03_WORLD_REPO is not configured")
    selected = Path(value).resolve(strict=True)
    if not selected.is_dir():
        pytest.skip("PMW_M03_WORLD_REPO is not a directory")
    return selected


def _physical_head(repo: Path) -> str:
    completed = subprocess.run(
        [
            "/usr/bin/git",
            f"--git-dir={repo}",
            "rev-parse",
            M03_WORLD_REF,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    return completed.stdout.decode("ascii", errors="strict").strip()


def _m03_host_writer() -> PmwWriterAuthority:
    # These are immutable capability identifiers from the copied world's
    # genesis, not filesystem or account credentials.
    return PmwWriterAuthority(
        channel_ref="channel/pmw-frontier-choice/host",
        invocation_ref="invocation/pmw-frontier-choice/host",
        process_ref="process/pmw-frontier-choice/host",
        principal_ref="principal/pmw-frontier-choice/host",
        episode_ref="episode/pmw-frontier-choice/host",
        capability_ref="capability/pmw-frontier-choice/host",
        scope_ref="scope/pmw-frontier-choice/public",
        policy_ref="policy/pmw-frontier-choice/1",
        policy_fingerprint=M03_POLICY_FINGERPRINT,
    )


def test_copied_m03_world_publish_delta_and_reopen(tmp_path: Path) -> None:
    source = _source_world()
    source_head_before = _physical_head(source)
    copied = tmp_path / "math-frontier.git"
    shutil.copytree(source, copied, symlinks=True)
    assert copied.resolve() != source

    world = ResearchWorld.open(
        copied,
        world_id="math-frontier",
        world_ref=M03_WORLD_REF,
        writer=_m03_host_writer(),
        required_snapshot_ref=M03_SNAPSHOT,
    )
    assert world.writable is True
    assert world.head() == M03_SNAPSHOT

    legacy = build_legacy_frontier_view(world, snapshot_ref=M03_SNAPSHOT)
    assert len(legacy.problems) == 14
    assert legacy.schema_counts["PMW_FRONTIER_TARGET_CARD_1"] == 14
    assert not hasattr(legacy, "publish")
    situation = build_mathematical_situation(
        world,
        world_id="math-frontier",
        snapshot_ref=M03_SNAPSHOT,
    )
    assert situation.value["problem_count"] == 14
    assert situation.value["admission_count"] == 174
    assert build_mathematical_situation(
        world,
        world_id="math-frontier",
        snapshot_ref=M03_SNAPSHOT,
    ).sha256 == situation.sha256

    selected = legacy.problems[0]
    plan = CohortPlan.generate(
        cohort_id="smoke-n1",
        world_id="math-frontier",
        world_ref=M03_WORLD_REF,
        base_snapshot_ref=M03_SNAPSHOT,
        safety_profile="research-default",
        safety_profile_sha256="1" * 64,
        core_lock_sha256="2" * 64,
        briefing_sha256=situation.sha256,
        count=1,
        concurrency=1,
    )
    contribution = ResearchContribution(
        kind="NOTE",
        problem_ids=(selected.problem_id,),
        parent_refs=(selected.target_card_admission_ref,),
        title="Model-free world continuity smoke",
        body=(
            "This deterministic note verifies direct continuation from the "
            "settled M03 snapshot without predecessor wrapping."
        ),
        payload={"model_calls": 0, "test": "N=1"},
    )
    record = contribution.bind(plan.sessions[0])
    published = world.bind_session(plan.sessions[0]).publish(contribution)
    assert published.base_snapshot_ref == M03_SNAPSHOT
    assert published.snapshot_ref == world.head()
    assert published.content_sha256 == record.content_sha256

    delta = world.delta(M03_SNAPSHOT)
    assert [row.admission_ref for row in delta] == [published.admission_ref]
    assert delta[0].content == record.to_value()
    assert world.get(published.admission_ref).content == record.to_value()

    # A new process may attach to the advanced world without any M0i import.
    reopened = ResearchWorld.open(
        copied,
        world_ref=M03_WORLD_REF,
        required_snapshot_ref=M03_SNAPSHOT,
    )
    assert reopened.writable is False
    assert reopened.head() == published.snapshot_ref
    assert reopened.get(published.admission_ref).content == record.to_value()
    reopened_delta = reopened.delta(M03_SNAPSHOT)
    assert [row.admission_ref for row in reopened_delta] == [
        published.admission_ref
    ]
    current_view = build_legacy_frontier_view(reopened)
    assert published.admission_ref in current_view.problem(
        selected.problem_id
    ).research_admission_refs

    # The source is immutable evidence; every write above targeted the copy.
    assert _physical_head(source) == source_head_before
