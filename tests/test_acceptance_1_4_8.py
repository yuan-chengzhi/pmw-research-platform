from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


pytest.importorskip(
    "pmw_r2.platform_admission",
    reason="the exact PMW core is an optional integration dependency",
)

from pmw_r2.platform_admission import (
    ADMIT,
    GET,
    LIST,
    AdmissionPolicy,
    FixtureCapability,
    initialize_git_world,
)

from pmw_platform.sessions import CohortPlan, SessionSpec, run_cohort
from pmw_platform.world import (
    PmwWriterAuthority,
    ResearchContribution,
    ResearchWorld,
)


WORLD_REF = "refs/pmw/research-world"
POLICY_REF = "policy/research-acceptance/1"
SCOPE_REF = "scope/research-acceptance/public"


def _world(tmp_path: Path) -> ResearchWorld:
    repo = tmp_path / "world.git"
    policy = AdmissionPolicy(
        policy_ref=POLICY_REF,
        maximum_content_bytes=65_536,
        maximum_page_items=64,
        maximum_query_bytes=256,
    )
    capability = FixtureCapability(
        capability_ref="capability/research-acceptance/host",
        principal_ref="principal/research-acceptance/host",
        episode_ref="episode/research-acceptance/host",
        scope_ref=SCOPE_REF,
        policy_ref=POLICY_REF,
        allowed_actions=frozenset({ADMIT, GET, LIST}),
    )
    initialize_git_world(
        repo,
        root_ref="root/research-acceptance",
        policy=policy,
        capabilities=(capability,),
        world_ref=WORLD_REF,
    )
    writer = PmwWriterAuthority(
        channel_ref="channel/research-acceptance/host",
        invocation_ref="invocation/research-acceptance/host",
        process_ref="process/research-acceptance/host",
        principal_ref=capability.principal_ref,
        episode_ref=capability.episode_ref,
        capability_ref=capability.capability_ref,
        scope_ref=capability.scope_ref,
        policy_ref=capability.policy_ref,
        policy_fingerprint=policy.fingerprint,
        maximum_calls=100,
        maximum_delivery_attempts=100,
    )
    return ResearchWorld.open(
        repo,
        world_id="acceptance-world",
        world_ref=WORLD_REF,
        writer=writer,
    )


def _plan(world: ResearchWorld, *, cohort: str, count: int, concurrency: int) -> CohortPlan:
    return CohortPlan.generate(
        cohort_id=cohort,
        world_id="acceptance-world",
        world_ref=WORLD_REF,
        base_snapshot_ref=world.head(),
        safety_profile="research-default",
        safety_profile_sha256="1" * 64,
        core_lock_sha256="2" * 64,
        briefing_sha256="3" * 64,
        count=count,
        concurrency=concurrency,
    )


def _contribution(spec: SessionSpec) -> ResearchContribution:
    return ResearchContribution(
        kind="CHECKPOINT",
        title=f"Zero-model checkpoint from {spec.session_id}",
        body="Deterministic acceptance record; no model was called.",
        payload={"model_calls": 0},
    )


async def _publish(world: ResearchWorld, spec: SessionSpec) -> dict[str, object]:
    publisher = world.bind_session(spec)
    result = await asyncio.to_thread(publisher.publish, _contribution(spec))
    return result.to_value()


def test_one_and_four_sessions_publish_from_explicit_shared_bases(tmp_path: Path) -> None:
    world = _world(tmp_path)

    one = _plan(world, cohort="acceptance-n1", count=1, concurrency=1)
    one_receipt = asyncio.run(run_cohort(one, lambda spec: _publish(world, spec)))
    assert len(one_receipt.succeeded) == 1
    assert len(world.delta(one.base_snapshot_ref)) == 1

    four = _plan(world, cohort="acceptance-n4", count=4, concurrency=4)
    four_receipt = asyncio.run(run_cohort(four, lambda spec: _publish(world, spec)))
    assert len(four_receipt.succeeded) == 4
    assert not four_receipt.failed
    delta = world.delta(four.base_snapshot_ref)
    assert len(delta) == 4
    assert len({row.admission_ref for row in delta}) == 4


def test_eight_sessions_isolate_failure_and_next_cohort_attaches_head(tmp_path: Path) -> None:
    world = _world(tmp_path)
    eight = _plan(world, cohort="acceptance-n8", count=8, concurrency=8)
    failed_id = eight.sessions[2].session_id

    async def worker(spec: SessionSpec) -> dict[str, object]:
        if spec.session_id == failed_id:
            raise RuntimeError("injected action-local failure")
        return await _publish(world, spec)

    receipt = asyncio.run(run_cohort(eight, worker))
    assert len(receipt.succeeded) == 7
    assert len(receipt.failed) == 1
    assert receipt.failed[0].session_id == failed_id
    assert len(world.delta(eight.base_snapshot_ref)) == 7

    head_after_eight = world.head()
    successor = _plan(world, cohort="acceptance-successor", count=1, concurrency=1)
    assert successor.base_snapshot_ref == head_after_eight
    successor_receipt = asyncio.run(
        run_cohort(successor, lambda spec: _publish(world, spec))
    )
    assert len(successor_receipt.succeeded) == 1
    assert len(world.delta(head_after_eight)) == 1
