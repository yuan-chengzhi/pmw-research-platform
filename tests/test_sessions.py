from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

import pytest

from pmw_platform.sessions import CohortPlan, SessionStatus, run_cohort


SNAPSHOT_A = "snapshot/sha256/" + "a" * 64
SNAPSHOT_B = "snapshot/sha256/" + "b" * 64
SNAPSHOT_C = "snapshot/sha256/" + "c" * 64
SNAPSHOT_D = "snapshot/sha256/" + "d" * 64
SNAPSHOT_E = "snapshot/sha256/" + "e" * 64


def _generate(**values: object) -> CohortPlan:
    return CohortPlan.generate(
        world_ref="refs/pmw/math-frontier",
        safety_profile_sha256="1" * 64,
        core_lock_sha256="2" * 64,
        briefing_sha256="3" * 64,
        **values,
    )


def test_four_sessions_run_at_concurrency_four_and_manifest_freezes_ids() -> None:
    plan = _generate(
        cohort_id="cohort-a",
        world_id="math-frontier",
        base_snapshot_ref=SNAPSHOT_A,
        safety_profile="research-default",
        count=4,
        concurrency=4,
    )
    manifest = plan.to_manifest()

    assert "count" not in manifest
    assert manifest["world_id"] == "math-frontier"
    assert manifest["safety_profile"] == "research-default"
    assert manifest["world_ref"] == "refs/pmw/math-frontier"
    assert all(set(item) == {"session_id"} for item in manifest["sessions"])
    assert [item["session_id"] for item in manifest["sessions"]] == [
        "cohort-a-session-0001",
        "cohort-a-session-0002",
        "cohort-a-session-0003",
        "cohort-a-session-0004",
    ]
    assert CohortPlan.from_manifest(manifest) == plan
    expanded = _generate(
        cohort_id="cohort-a",
        world_id="math-frontier",
        base_snapshot_ref=SNAPSHOT_A,
        safety_profile="research-default",
        count=8,
        concurrency=4,
    )
    assert expanded.sessions[:4] == plan.sessions

    async def scenario() -> None:
        entered = 0
        all_entered = asyncio.Event()

        async def worker(spec):
            nonlocal entered
            entered += 1
            if entered == 4:
                all_entered.set()
            await asyncio.wait_for(all_entered.wait(), timeout=1)
            return spec.session_id

        receipt = await run_cohort(plan, worker)
        assert len(receipt.succeeded) == 4
        assert not receipt.failed
        assert [item.result for item in receipt.receipts] == [
            spec.session_id for spec in plan.sessions
        ]

    asyncio.run(scenario())


def test_eight_sessions_at_concurrency_eight_isolate_one_failure() -> None:
    plan = _generate(
        cohort_id="cohort-b",
        world_id="math-frontier",
        base_snapshot_ref=SNAPSHOT_B,
        safety_profile="research-default",
        count=8,
        concurrency=8,
    )
    failing_id = plan.sessions[3].session_id

    async def worker(spec):
        await asyncio.sleep(0)
        if spec.session_id == failing_id:
            raise RuntimeError("local research failure")
        return {"completed": spec.session_id}

    receipt = asyncio.run(run_cohort(plan, worker))

    assert len(receipt.receipts) == 8
    assert len(receipt.succeeded) == 7
    assert len(receipt.failed) == 1
    assert all(item.cohort_id == plan.cohort_id for item in receipt.receipts)
    assert all(item.world_id == plan.world_id for item in receipt.receipts)
    assert all(item.world_ref == plan.world_ref for item in receipt.receipts)
    assert all(item.plan_sha256 == plan.sha256 for item in receipt.receipts)
    assert all(item.base_snapshot_ref == plan.base_snapshot_ref for item in receipt.receipts)
    assert all(
        item.safety_profile == plan.safety_profile for item in receipt.receipts
    )
    failed = receipt.for_session(failing_id)
    assert failed.status is SessionStatus.FAILED
    assert failed.error_type == "builtins.RuntimeError"
    assert failed.error_message == "local research failure"
    assert all(item.result is not None for item in receipt.succeeded)


def test_eight_sessions_with_concurrency_three_never_exceed_bound() -> None:
    plan = _generate(
        cohort_id="cohort-c",
        world_id="math-frontier",
        base_snapshot_ref=SNAPSHOT_C,
        safety_profile="research-default",
        count=8,
        concurrency=3,
    )

    async def scenario() -> tuple[int, int]:
        active = 0
        peak = 0
        completed = 0

        async def worker(_spec):
            nonlocal active, peak, completed
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            completed += 1
            return completed

        receipt = await run_cohort(plan, worker)
        assert len(receipt.succeeded) == 8
        return peak, completed

    peak, completed = asyncio.run(scenario())
    assert completed == 8
    assert peak == 3


def test_caller_can_start_next_cohort_from_a_new_base() -> None:
    first = _generate(
        cohort_id="cohort-d1",
        world_id="math-frontier",
        base_snapshot_ref=SNAPSHOT_D,
        safety_profile="research-default",
        count=4,
        concurrency=2,
    )
    next_base = SNAPSHOT_E
    second = _generate(
        cohort_id="cohort-d2",
        world_id="math-frontier",
        base_snapshot_ref=next_base,
        safety_profile="research-default",
        count=4,
        concurrency=2,
    )

    assert first.base_snapshot_ref == SNAPSHOT_D
    assert all(spec.world_id == "math-frontier" for spec in second.sessions)
    assert all(spec.base_snapshot_ref == next_base for spec in second.sessions)
    assert all(
        spec.safety_profile == "research-default" for spec in second.sessions
    )
    assert {spec.session_id for spec in first.sessions}.isdisjoint(
        spec.session_id for spec in second.sessions
    )


def test_worker_local_cancel_and_broken_error_string_are_isolated() -> None:
    plan = _generate(
        cohort_id="cohort-errors",
        world_id="math-frontier",
        base_snapshot_ref=SNAPSHOT_A,
        safety_profile="research-default",
        count=3,
        concurrency=3,
    )

    class BrokenMessage(RuntimeError):
        def __str__(self) -> str:
            raise RuntimeError("broken __str__")

    async def worker(spec):
        if spec.session_id.endswith("0001"):
            raise asyncio.CancelledError()
        if spec.session_id.endswith("0002"):
            raise BrokenMessage()
        return "ok"

    receipt = asyncio.run(run_cohort(plan, worker))
    assert receipt.receipts[0].status is SessionStatus.CANCELLED
    assert receipt.receipts[1].status is SessionStatus.FAILED
    assert receipt.receipts[1].error_message == "<exception message unavailable>"
    assert receipt.receipts[2].status is SessionStatus.SUCCEEDED


def test_worker_failure_before_awaitable_creation_is_isolated() -> None:
    plan = _generate(
        cohort_id="cohort-sync-errors",
        world_id="math-frontier",
        base_snapshot_ref=SNAPSHOT_A,
        safety_profile="research-default",
        count=3,
        concurrency=3,
    )

    def worker(spec):
        if spec.session_id.endswith("0001"):
            raise RuntimeError("synchronous worker failure")
        if spec.session_id.endswith("0002"):
            return "not awaitable"

        async def succeed():
            await asyncio.sleep(0)
            return "ok"

        return succeed()

    receipt = asyncio.run(run_cohort(plan, worker))
    assert [item.status for item in receipt.receipts] == [
        SessionStatus.FAILED,
        SessionStatus.FAILED,
        SessionStatus.SUCCEEDED,
    ]
    assert receipt.receipts[0].error_message == "synchronous worker failure"
    assert receipt.receipts[1].error_type == "builtins.TypeError"


def test_worker_may_return_a_future_as_promised_by_awaitable_contract() -> None:
    plan = _generate(
        cohort_id="cohort-future",
        world_id="math-frontier",
        base_snapshot_ref=SNAPSHOT_A,
        safety_profile="research-default",
        count=1,
        concurrency=1,
    )

    def worker(_spec):
        future = asyncio.get_running_loop().create_future()
        future.set_result("future result")
        return future

    receipt = asyncio.run(run_cohort(plan, worker))
    assert receipt.receipts[0].status is SessionStatus.SUCCEEDED
    assert receipt.receipts[0].result == "future result"


def test_external_cancel_waits_for_constructed_receipt_to_persist() -> None:
    plan = _generate(
        cohort_id="cancel-during-settlement",
        world_id="math-frontier",
        base_snapshot_ref=SNAPSHOT_A,
        safety_profile="research-default",
        count=1,
        concurrency=1,
    )

    async def scenario() -> list[SessionStatus]:
        sink_entered = asyncio.Event()
        release_sink = asyncio.Event()
        persisted: list[SessionStatus] = []

        async def worker(_spec):
            return "settled"

        async def sink(receipt) -> None:
            sink_entered.set()
            await release_sink.wait()
            persisted.append(receipt.status)

        running = asyncio.create_task(
            run_cohort(plan, worker, receipt_sink=sink)
        )
        await asyncio.wait_for(sink_entered.wait(), timeout=1)
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        release_sink.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        return persisted

    assert asyncio.run(scenario()) == [SessionStatus.SUCCEEDED]


def test_count_and_concurrency_are_rejected_before_allocation() -> None:
    with pytest.raises(ValueError, match="host capacity"):
        _generate(
            cohort_id="too-many",
            world_id="math-frontier",
            base_snapshot_ref=SNAPSHOT_A,
            safety_profile="research-default",
            count=4_097,
            concurrency=1,
        )
    with pytest.raises(ValueError, match="between 1 and count"):
        _generate(
            cohort_id="bad-concurrency",
            world_id="math-frontier",
            base_snapshot_ref=SNAPSHOT_A,
            safety_profile="research-default",
            count=4_096,
            concurrency=4_097,
        )


def test_published_schema_matches_the_compact_manifest_shape() -> None:
    plan = _generate(
        cohort_id="schema-check",
        world_id="math-frontier",
        base_snapshot_ref=SNAPSHOT_A,
        safety_profile="research-default",
        count=2,
        concurrency=1,
    )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "cohort.schema.json"
        ).read_text()
    )
    manifest = plan.to_manifest()
    assert set(schema["required"]) == set(manifest)
    assert set(schema["properties"]) == set(manifest)
    item_schema = schema["properties"]["sessions"]["items"]
    assert set(item_schema["required"]) == {"session_id"}
    assert set(item_schema["properties"]) == {"session_id"}


def test_external_cancel_does_not_claim_a_background_thread_stopped() -> None:
    plan = _generate(
        cohort_id="cancel-thread",
        world_id="math-frontier",
        base_snapshot_ref=SNAPSHOT_A,
        safety_profile="research-default",
        count=1,
        concurrency=1,
    )

    async def scenario() -> tuple[list[SessionStatus], list[str]]:
        statuses: list[SessionStatus] = []
        effects: list[str] = []

        def blocking_work() -> str:
            time.sleep(0.08)
            effects.append("committed")
            return "done"

        async def worker(_spec):
            return await asyncio.to_thread(blocking_work)

        async def sink(receipt) -> None:
            statuses.append(receipt.status)

        running = asyncio.create_task(
            run_cohort(
                plan,
                worker,
                receipt_sink=sink,
                cancellation_grace_seconds=0.01,
            )
        )
        await asyncio.sleep(0.01)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.sleep(0.1)
        return statuses, effects

    statuses, effects = asyncio.run(scenario())
    assert statuses == [SessionStatus.UNKNOWN]
    assert effects == ["committed"]
