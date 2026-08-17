from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from pmw_platform.runtime.auth import PreparedCohort
from pmw_platform.runtime.contracts import (
    BackendIdentity,
    BackendOutcome,
    StopProof,
)
from pmw_platform.runtime.context import (
    ContextWindowControl,
    ContextWindowPolicy,
)
from pmw_platform.runtime.orchestrator import (
    RuntimeLimits,
    RuntimeOrchestrationError,
    run_prepared_cohort,
    run_runtime_cohort,
)
from pmw_platform.runtime import resource_guard as resource_guard_module
from pmw_platform.runtime.resource_guard import DiskSnapshot
from pmw_platform.runtime.publish import PublicationIdentity
from pmw_platform.runtime.safety import load_named_profile
from pmw_platform.runtime.safety import TreeLimits
from pmw_platform.runtime.store import RuntimeStore, RuntimeStoreError
from pmw_platform.sessions import CohortPlan
from pmw_platform.source_lock import load_core_lock
from pmw_platform.world import ResearchContribution


SNAPSHOT = "snapshot/sha256/" + "a" * 64


class _Publisher:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.identity = PublicationIdentity(
            mode="TEST_PUBLISHER",
            protocol="TEST_PUBLICATION_1",
            public_config={"implementation": "tests"},
        )

    def __call__(self, spec, contribution):
        return self.callback(spec, contribution)


def _prepared(
    tmp_path: Path,
    *,
    cohort_id: str,
    count: int,
    concurrency: int,
) -> PreparedCohort:
    profile = load_named_profile("research-default")
    core = load_core_lock()
    briefing = b'{"schema":"TEST_BRIEFING_1"}\n'
    plan = CohortPlan.generate(
        cohort_id=cohort_id,
        world_id="math-frontier",
        world_ref="refs/pmw/math-frontier",
        base_snapshot_ref=SNAPSHOT,
        safety_profile=profile.name,
        safety_profile_sha256=profile.sha256,
        core_lock_sha256=core.sha256,
        briefing_sha256=hashlib.sha256(briefing).hexdigest(),
        count=count,
        concurrency=concurrency,
    )
    cohort_root = tmp_path / "runs" / cohort_id
    cohort_root.mkdir(parents=True)
    return PreparedCohort(
        data_root=tmp_path,
        cohort_root=cohort_root,
        plan_path=cohort_root / "plan.json",
        briefing_path=cohort_root / "briefing.json",
        briefing_bytes=briefing,
        plan=plan,
        profile=profile,
        core_lock=core,
        registration=SimpleNamespace(
            name="math-frontier",
            repo=str(tmp_path / "world.git"),
            world_ref="refs/pmw/math-frontier",
        ),
        world=SimpleNamespace(),
        artifact_store=SimpleNamespace(exists=lambda _ref: True),
    )


class _FakeHandle:
    def __init__(
        self,
        backend: "_FakeBackend",
        outcome: BackendOutcome,
        *,
        stop_is_proven: bool,
    ) -> None:
        self.backend = backend
        self.outcome = outcome
        self.stop_is_proven = stop_is_proven
        self.stopped = False

    async def wait(self) -> BackendOutcome:
        if self.backend.release is not None:
            await self.backend.release.wait()
        else:
            await asyncio.sleep(self.backend.delay)
        return self.outcome

    async def stop(self, reason: str, grace_seconds: float) -> object:
        del grace_seconds
        if not self.stopped:
            self.stopped = True
            self.backend.active -= 1
        if self.backend.invalid_stop:
            return None
        return StopProof(
            stopped=self.stop_is_proven,
            reason=reason,
            process_group_id=None,
        )


class _FakeBackend:
    def __init__(
        self,
        *,
        delay: float = 0.01,
        release: asyncio.Event | None = None,
        failure_suffix: str | None = None,
        unknown_suffix: str | None = None,
        contribution: ResearchContribution | None = None,
        invalid_stop: bool = False,
        context_window_control: ContextWindowControl = (
            ContextWindowControl.NATIVE_MODEL_WINDOW
        ),
    ) -> None:
        self._identity = BackendIdentity(
            name="fake-runtime",
            protocol="FAKE_RUNTIME_1",
            public_config={"implementation": "tests"},
        )
        self.delay = delay
        self.release = release
        self.failure_suffix = failure_suffix
        self.unknown_suffix = unknown_suffix
        self.contribution = contribution
        self.invalid_stop = invalid_stop
        self.active = 0
        self.peak = 0
        self.started: list[str] = []
        self.context_windows: dict[str, int | None] = {}
        self._context_window_control = context_window_control
        self.first_started = asyncio.Event()

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    @property
    def context_window_control(self) -> ContextWindowControl:
        return self._context_window_control

    def verify_runtime(self) -> None:
        return None

    async def start(self, request):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.append(request.spec.session_id)
        self.context_windows[request.spec.session_id] = (
            request.context_window_tokens
        )
        self.first_started.set()
        failed = (
            self.failure_suffix is not None
            and request.spec.session_id.endswith(self.failure_suffix)
        )
        outcome = BackendOutcome(
            success=not failed,
            terminal_reason="RESEARCH_FAILED" if failed else "COMPLETED",
            summary="fake outcome",
            contributions=(
                () if self.contribution is None else (self.contribution,)
            ),
        )
        return _FakeHandle(
            self,
            outcome,
            stop_is_proven=not (
                self.unknown_suffix is not None
                and request.spec.session_id.endswith(self.unknown_suffix)
            ),
        )


@pytest.mark.parametrize("count,concurrency", [(1, 1), (4, 4), (8, 3)])
def test_runtime_uses_exact_plan_concurrency_and_settles_every_session(
    tmp_path: Path,
    count: int,
    concurrency: int,
) -> None:
    prepared = _prepared(
        tmp_path,
        cohort_id=f"cohort-{count}-{concurrency}",
        count=count,
        concurrency=concurrency,
    )
    backend = _FakeBackend()

    result = asyncio.run(run_prepared_cohort(prepared, backend))

    assert result.outcome == "SUCCEEDED"
    assert len(result.receipts) == count
    assert backend.peak == concurrency
    assert backend.active == 0
    status = RuntimeStore(prepared.cohort_root).read_status()
    assert status["settled"] is True
    assert all(row["receipt_status"] == "SUCCEEDED" for row in status["sessions"])
    launch = RuntimeStore(prepared.cohort_root).read_launch()
    assert launch["publication"]["mode"] == "DISABLED"  # type: ignore[index]


def test_local_failure_isolated_from_peer_sessions(
    tmp_path: Path,
) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-local-failure", count=4, concurrency=2
    )
    backend = _FakeBackend(failure_suffix="0002")

    result = asyncio.run(run_prepared_cohort(prepared, backend))

    assert result.outcome == "COMPLETED_WITH_FAILURES"
    assert [row["status"] for row in result.receipts].count("FAILED") == 1
    assert [row["status"] for row in result.receipts].count("SUCCEEDED") == 3


def test_context_window_default_and_session_override_are_launch_bound(
    tmp_path: Path,
) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-context", count=3, concurrency=2
    )
    backend = _FakeBackend()
    overridden = prepared.plan.sessions[1].session_id
    policy = ContextWindowPolicy(
        default_tokens=400_000,
        session_overrides={overridden: 360_000},
    )

    result = asyncio.run(
        run_prepared_cohort(prepared, backend, context_policy=policy)
    )

    assert result.outcome == "SUCCEEDED"
    assert backend.context_windows == {
        spec.session_id: 360_000 if spec.session_id == overridden else 400_000
        for spec in prepared.plan.sessions
    }
    launch = RuntimeStore(prepared.cohort_root).read_launch()
    assert launch["backend_context_window_control"] == "NATIVE_MODEL_WINDOW"
    assert launch["context_window_policy"] == policy.bind(
        [spec.session_id for spec in prepared.plan.sessions]
    )
    assert [
        receipt["context_window"]["configured_tokens"]  # type: ignore[index]
        for receipt in result.receipts
    ] == [400_000, 360_000, 400_000]


def test_required_readiness_is_rechecked_and_bound_into_launch(
    tmp_path: Path,
) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-readiness", count=1, concurrency=1
    )
    backend = _FakeBackend()

    class Checker:
        name = "fixture-apparatus"

        def __init__(self) -> None:
            self.calls = 0

        def verify(self, **values):
            self.calls += 1
            assert not (prepared.cohort_root / "runtime").exists()
            assert values["prepared"] is prepared
            assert values["backend"] is backend
            return {"target_count": 14, "portfolio_sha256": "a" * 64}

    checker = Checker()
    result = asyncio.run(
        run_prepared_cohort(
            prepared,
            backend,
            required_checkers=(checker,),
        )
    )

    assert result.outcome == "SUCCEEDED"
    assert checker.calls == 1
    launch = RuntimeStore(prepared.cohort_root).read_launch()
    assert launch["required_readiness"]["checks"] == [
        {
            "name": "fixture-apparatus",
            "evidence": {
                "target_count": 14,
                "portfolio_sha256": "a" * 64,
            },
        }
    ]
    assert len(launch["required_readiness_sha256"]) == 64


def test_public_runtime_entrypoint_forwards_context_and_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-public-entrypoint", count=2, concurrency=1
    )
    backend = _FakeBackend()
    policy = ContextWindowPolicy(default_tokens=400_000)
    checker = object()
    observed: dict[str, object] = {}
    expected = object()

    def fake_authenticate(data_root, cohort_id, **kwargs):
        observed["authenticate"] = (data_root, cohort_id, kwargs)
        return prepared

    async def fake_run(selected_prepared, selected_backend, **kwargs):
        observed["run"] = (selected_prepared, selected_backend, kwargs)
        return expected

    monkeypatch.setattr(
        "pmw_platform.runtime.orchestrator.authenticate_plan_bundle",
        fake_authenticate,
    )
    monkeypatch.setattr(
        "pmw_platform.runtime.orchestrator.run_prepared_cohort",
        fake_run,
    )

    result = asyncio.run(
        run_runtime_cohort(
            tmp_path,
            prepared.plan.cohort_id,
            backend,
            context_policy=policy,
            required_checkers=(checker,),  # type: ignore[arg-type]
        )
    )

    assert result is expected
    assert observed["authenticate"] == (
        tmp_path,
        prepared.plan.cohort_id,
        {"profiles_dir": None, "core_lock_path": None},
    )
    assert observed["run"] == (
        prepared,
        backend,
        {
            "limits": None,
            "context_policy": policy,
            "publisher": None,
            "required_checkers": (checker,),
            "verifier_kit": None,
        },
    )


def test_context_window_rejects_unsupported_backend_before_launch(
    tmp_path: Path,
) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-context-unsupported", count=1, concurrency=1
    )
    backend = _FakeBackend(
        context_window_control=ContextWindowControl.NOT_APPLICABLE
    )

    with pytest.raises(
        RuntimeOrchestrationError,
        match="CONTEXT_WINDOW_CONTROL_UNSUPPORTED",
    ):
        asyncio.run(
            run_prepared_cohort(
                prepared,
                backend,
                context_policy=ContextWindowPolicy(default_tokens=400_000),
            )
        )

    assert not (prepared.cohort_root / "runtime").exists()


def test_backend_identity_drift_after_start_stops_returned_handle(
    tmp_path: Path,
) -> None:
    prepared = _prepared(
        tmp_path,
        cohort_id="cohort-backend-identity-drift",
        count=1,
        concurrency=1,
    )
    backend = _FakeBackend()
    original_start = backend.start
    returned_handles: list[_FakeHandle] = []

    async def drifting_start(request):
        handle = await original_start(request)
        returned_handles.append(handle)
        backend._identity = BackendIdentity(
            name="fake-runtime",
            protocol="FAKE_RUNTIME_1",
            public_config={"implementation": "drifted-after-start"},
        )
        return handle

    backend.start = drifting_start  # type: ignore[method-assign]

    result = asyncio.run(run_prepared_cohort(prepared, backend))

    assert result.outcome == "COMPLETED_WITH_FAILURES"
    assert len(returned_handles) == 1
    assert returned_handles[0].stopped is True
    assert backend.active == 0
    receipt = result.receipts[0]
    assert receipt["status"] == "FAILED"
    assert receipt["terminal_reason"] == "BACKEND_IDENTITY_DRIFT"
    assert receipt["error"]["code"] == (  # type: ignore[index]
        "BACKEND_IDENTITY_DRIFT"
    )
    assert receipt["stop_proof"]["stopped"] is True  # type: ignore[index]
    assert receipt["stop_proof"]["reason"] == (  # type: ignore[index]
        "BACKEND_IDENTITY_DRIFT"
    )


def test_startup_timeout_stops_a_late_handle_after_cancel_is_swallowed(
    tmp_path: Path,
) -> None:
    async def scenario():
        prepared = _prepared(
            tmp_path,
            cohort_id="cohort-late-start-handle",
            count=1,
            concurrency=1,
        )
        backend = _FakeBackend()
        handle = _FakeHandle(
            backend,
            BackendOutcome(
                success=True,
                terminal_reason="COMPLETED",
                summary="must never become a running result",
            ),
            stop_is_proven=True,
        )
        cancellation_seen = asyncio.Event()
        start_finished = asyncio.Event()
        start_tasks: list[asyncio.Task[object]] = []
        never = asyncio.Event()

        async def late_start(request):
            current = asyncio.current_task()
            assert current is not None
            start_tasks.append(current)
            backend.active += 1
            backend.peak = max(backend.peak, backend.active)
            backend.started.append(request.spec.session_id)
            backend.first_started.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                # Simulate a contract-violating adapter which converts task
                # cancellation into a late, externally active handle.
                await asyncio.sleep(0.01)
                return handle
            finally:
                start_finished.set()

        backend.start = late_start  # type: ignore[method-assign]
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await run_prepared_cohort(
            prepared,
            backend,
            limits=RuntimeLimits(
                startup_seconds=0.02,
                session_wall_seconds=None,
            ),
        )
        return (
            result,
            backend,
            handle,
            cancellation_seen.is_set(),
            start_finished.is_set(),
            all(task.done() for task in start_tasks),
            loop.time() - started,
        )

    (
        result,
        backend,
        handle,
        cancellation_seen,
        start_finished,
        start_tasks_done,
        elapsed,
    ) = asyncio.run(scenario())

    assert result.outcome == "COMPLETED_WITH_FAILURES"
    assert cancellation_seen is True
    assert start_finished is True
    assert start_tasks_done is True
    assert handle.stopped is True
    assert backend.active == 0
    assert elapsed < 1.0
    receipt = result.receipts[0]
    assert receipt["status"] == "FAILED"
    assert receipt["stop_proof"]["stopped"] is True  # type: ignore[index]
    assert receipt["stop_proof"]["reason"] == (  # type: ignore[index]
        "START_TIMEOUT"
    )


def test_live_disk_breach_interrupts_start_and_joins_late_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario():
        prepared = _prepared(
            tmp_path,
            cohort_id="cohort-live-disk-during-start",
            count=1,
            concurrency=1,
        )
        prepared = replace(
            prepared,
            profile=replace(
                prepared.profile,
                disk_guard=replace(
                    prepared.profile.disk_guard,
                    poll_interval_seconds=0.01,
                ),
            ),
        )
        backend = _FakeBackend()
        handle = _FakeHandle(
            backend,
            BackendOutcome(
                success=True,
                terminal_reason="COMPLETED",
                summary="must be stopped by the live disk guard",
            ),
            stop_is_proven=True,
        )
        cancellation_seen = asyncio.Event()
        start_tasks: list[asyncio.Task[object]] = []
        never = asyncio.Event()

        async def start_until_disk_breach(request):
            current = asyncio.current_task()
            assert current is not None
            start_tasks.append(current)
            backend.active += 1
            backend.peak = max(backend.peak, backend.active)
            backend.started.append(request.spec.session_id)
            backend.first_started.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await asyncio.sleep(0)
                return handle

        backend.start = start_until_disk_breach  # type: ignore[method-assign]
        disk_checks = 0

        async def disk_turns_low_after_start(_path, _guard):
            nonlocal disk_checks
            disk_checks += 1
            if disk_checks == 1:
                return DiskSnapshot(
                    total_bytes=100,
                    available_bytes=100,
                    required_free_bytes=10,
                )
            await backend.first_started.wait()
            return DiskSnapshot(
                total_bytes=100,
                available_bytes=1,
                required_free_bytes=10,
            )

        monkeypatch.setattr(
            resource_guard_module,
            "read_disk_snapshot",
            disk_turns_low_after_start,
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await run_prepared_cohort(
            prepared,
            backend,
            limits=RuntimeLimits(
                startup_seconds=10.0,
                session_wall_seconds=None,
            ),
        )
        return (
            result,
            backend,
            handle,
            cancellation_seen.is_set(),
            all(task.done() for task in start_tasks),
            loop.time() - started,
        )

    (
        result,
        backend,
        handle,
        cancellation_seen,
        start_tasks_done,
        elapsed,
    ) = asyncio.run(scenario())

    assert result.outcome == "CANCELLED"
    assert cancellation_seen is True
    assert start_tasks_done is True
    assert handle.stopped is True
    assert backend.active == 0
    assert elapsed < 1.0
    receipt = result.receipts[0]
    assert receipt["status"] == "CANCELLED"
    assert receipt["terminal_reason"] == "DISK_RESERVE_BREACHED"
    assert receipt["error"]["code"] == (  # type: ignore[index]
        "DISK_RESERVE_BREACHED"
    )
    assert receipt["stop_proof"]["stopped"] is True  # type: ignore[index]
    assert receipt["stop_proof"]["reason"] == (  # type: ignore[index]
        "DISK_RESERVE_BREACHED"
    )


def test_unproven_cleanup_stops_new_work_and_does_not_reuse_slot(
    tmp_path: Path,
) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-unknown", count=4, concurrency=1
    )
    backend = _FakeBackend(unknown_suffix="0001")

    result = asyncio.run(run_prepared_cohort(prepared, backend))

    assert result.outcome == "UNSAFE"
    assert backend.started == [prepared.plan.sessions[0].session_id]
    assert [row["status"] for row in result.receipts] == [
        "UNKNOWN",
        "CANCELLED",
        "CANCELLED",
        "CANCELLED",
    ]
    assert all(
        row["terminal_reason"] == "NOT_STARTED"
        for row in result.receipts[1:]
    )


def test_malformed_stop_return_is_unknown_and_settles_unsafe(
    tmp_path: Path,
) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-invalid-stop", count=2, concurrency=1
    )
    backend = _FakeBackend(invalid_stop=True)

    result = asyncio.run(run_prepared_cohort(prepared, backend))

    assert result.outcome == "UNSAFE"
    assert backend.started == [prepared.plan.sessions[0].session_id]
    assert [row["status"] for row in result.receipts] == [
        "UNKNOWN",
        "CANCELLED",
    ]
    proof = result.receipts[0]["stop_proof"]
    assert type(proof) is dict
    assert proof["stopped"] is False
    assert proof["reason"] == "STOP_UNPROVEN"
    assert proof["detail"] == "invalid stop proof type: builtins.NoneType"


def test_external_cancel_proves_active_stop_and_settles_queued_sessions(
    tmp_path: Path,
) -> None:
    async def scenario() -> PreparedCohort:
        release = asyncio.Event()
        prepared = _prepared(
            tmp_path, cohort_id="cohort-cancel", count=4, concurrency=1
        )
        backend = _FakeBackend(release=release)
        task = asyncio.create_task(
            run_prepared_cohort(
                prepared,
                backend,
                limits=RuntimeLimits(session_wall_seconds=None),
            )
        )
        await asyncio.wait_for(backend.first_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.active == 0
        return prepared

    prepared = asyncio.run(scenario())
    settlement = RuntimeStore(prepared.cohort_root).read_settlement()
    assert settlement is not None
    assert settlement["outcome"] == "CANCELLED"
    assert settlement["counts"]["CANCELLED"] == 4


def test_external_cancel_never_masks_an_unproven_stop(tmp_path: Path) -> None:
    async def scenario() -> PreparedCohort:
        release = asyncio.Event()
        prepared = _prepared(
            tmp_path, cohort_id="cohort-cancel-unknown", count=2, concurrency=1
        )
        backend = _FakeBackend(release=release, unknown_suffix="0001")
        task = asyncio.create_task(
            run_prepared_cohort(
                prepared,
                backend,
                limits=RuntimeLimits(session_wall_seconds=None),
            )
        )
        await asyncio.wait_for(backend.first_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return prepared

    prepared = asyncio.run(scenario())
    settlement = RuntimeStore(prepared.cohort_root).read_settlement()
    assert settlement is not None
    assert settlement["outcome"] == "UNSAFE"
    assert settlement["counts"]["UNKNOWN"] == 1
    assert settlement["counts"]["CANCELLED"] == 1


def test_backend_contribution_is_published_with_host_selected_spec(
    tmp_path: Path,
) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-publish", count=1, concurrency=1
    )
    contribution = ResearchContribution(
        kind="ATTEMPT",
        problem_ids=("degree-diameter-3-9",),
        title="One route",
        body="Identity is not supplied by this contribution.",
    )
    backend = _FakeBackend(contribution=contribution)
    observed: list[tuple[str, str]] = []

    def publish(spec, proposed):
        observed.append((spec.session_id, proposed.title))
        return {"admission_ref": "admission/sha256/" + "b" * 64}

    result = asyncio.run(
        run_prepared_cohort(prepared, backend, publisher=_Publisher(publish))
    )

    assert result.outcome == "SUCCEEDED"
    assert observed == [(prepared.plan.sessions[0].session_id, "One route")]
    assert result.receipts[0]["publications"] == [
        {"admission_ref": "admission/sha256/" + "b" * 64}
    ]
    launch = RuntimeStore(prepared.cohort_root).read_launch()
    assert launch["publication"]["mode"] == "TEST_PUBLISHER"  # type: ignore[index]


def test_publisher_requires_a_frozen_public_identity(tmp_path: Path) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-publisher-identity", count=1, concurrency=1
    )

    with pytest.raises(TypeError, match="publisher.identity"):
        asyncio.run(
            run_prepared_cohort(
                prepared,
                _FakeBackend(),
                publisher=lambda _spec, _item: None,
            )
        )

    assert not (prepared.cohort_root / "runtime").exists()


def test_publication_identity_drift_fails_before_admission(tmp_path: Path) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-publisher-drift", count=1, concurrency=1
    )
    contribution = ResearchContribution(
        kind="NOTE", title="identity drift", body="must not be admitted"
    )
    backend = _FakeBackend(contribution=contribution)

    class DriftingPublisher:
        def __init__(self) -> None:
            self.reads = 0
            self.called = False

        @property
        def identity(self) -> PublicationIdentity:
            self.reads += 1
            return PublicationIdentity(
                mode="TEST_PUBLISHER",
                protocol="TEST_PUBLICATION_1",
                public_config={"revision": min(self.reads, 2)},
            )

        def __call__(self, _spec, _item):
            self.called = True

    publisher = DriftingPublisher()
    result = asyncio.run(
        run_prepared_cohort(prepared, backend, publisher=publisher)
    )

    assert result.outcome == "COMPLETED_WITH_FAILURES"
    assert result.receipts[0]["error"]["code"] == "PUBLICATION_IDENTITY_DRIFT"  # type: ignore[index]
    assert publisher.called is False


def test_partial_publication_is_never_hidden_by_a_later_failure(
    tmp_path: Path,
) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-partial-publish", count=1, concurrency=1
    )
    first = ResearchContribution(
        kind="NOTE", title="first", body="durably accepted first"
    )
    second = ResearchContribution(
        kind="NOTE", title="second", body="rejected later"
    )
    backend = _FakeBackend()

    async def start_with_two(request):
        backend.active += 1
        backend.peak = 1
        backend.started.append(request.spec.session_id)
        return _FakeHandle(
            backend,
            BackendOutcome(
                success=True,
                terminal_reason="COMPLETED",
                summary="two proposals",
                contributions=(first, second),
            ),
            stop_is_proven=True,
        )

    backend.start = start_with_two  # type: ignore[method-assign]
    calls = 0

    def publish(_spec, contribution):
        nonlocal calls
        calls += 1
        if contribution.title == "second":
            raise RuntimeError("second admission rejected")
        return {"admission_ref": "admission/sha256/" + "c" * 64}

    result = asyncio.run(
        run_prepared_cohort(prepared, backend, publisher=_Publisher(publish))
    )

    assert result.outcome == "COMPLETED_WITH_FAILURES"
    assert calls == 2
    assert result.receipts[0]["status"] == "FAILED"
    assert result.receipts[0]["publications"] == [
        {"admission_ref": "admission/sha256/" + "c" * 64}
    ]


def test_a_settled_cohort_is_never_silently_relaunched(tmp_path: Path) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-no-replay", count=1, concurrency=1
    )
    asyncio.run(run_prepared_cohort(prepared, _FakeBackend()))

    with pytest.raises(RuntimeStoreError) as caught:
        asyncio.run(run_prepared_cohort(prepared, _FakeBackend()))
    assert caught.value.code == "RUNTIME_PATH_OCCUPIED"


def test_terminal_aggregate_limit_blocks_success_before_publication(
    tmp_path: Path,
) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-resource-limit", count=1, concurrency=1
    )
    tiny_workspace = TreeLimits(
        maximum_total_bytes=1,
        maximum_entries=100,
        maximum_file_bytes=None,
        maximum_depth=16,
        scan_mode="QUIESCENT",
        live_scan_interval_seconds=None,
    )
    prepared = replace(
        prepared,
        profile=replace(prepared.profile, workspace=tiny_workspace),
    )
    backend = _FakeBackend(
        contribution=ResearchContribution(
            kind="NOTE",
            title="must not publish",
            body="Resource settlement precedes publication.",
        )
    )
    original_start = backend.start

    async def start_with_workspace_result(request):
        request.workspace.joinpath("result").write_bytes(b"too large")
        return await original_start(request)

    backend.start = start_with_workspace_result  # type: ignore[method-assign]
    publications: list[object] = []

    result = asyncio.run(
        run_prepared_cohort(
            prepared,
            backend,
            publisher=_Publisher(
                lambda _spec, item: publications.append(item)
            ),
        )
    )

    assert result.outcome == "COMPLETED_WITH_FAILURES"
    assert result.receipts[0]["status"] == "FAILED"
    assert result.receipts[0]["terminal_reason"] == (
        "WORKSPACE_TOTAL_BYTES_EXCEEDED"
    )
    evidence = result.receipts[0]["resource_guard"]
    assert evidence["terminal_event"]["phase"] == "TERMINAL"  # type: ignore[index]
    assert publications == []
    assert backend.active == 0


def test_initial_disk_reserve_breach_starts_no_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-disk-reserve", count=2, concurrency=1
    )
    backend = _FakeBackend()

    async def low_disk(_path, _guard):
        return DiskSnapshot(
            total_bytes=100,
            available_bytes=1,
            required_free_bytes=10,
        )

    monkeypatch.setattr(resource_guard_module, "read_disk_snapshot", low_disk)
    result = asyncio.run(run_prepared_cohort(prepared, backend))

    assert result.outcome == "CANCELLED"
    assert backend.started == []
    assert [row["status"] for row in result.receipts] == [
        "CANCELLED",
        "CANCELLED",
    ]
    assert all(
        row["terminal_reason"] == "DISK_RESERVE_BREACHED"
        for row in result.receipts
    )
    assert all(
        row["resource_guard"]["terminal_event"]["code"]
        == "DISK_RESERVE_BREACHED"
        for row in result.receipts
    )
    assert all(
        row["resource_guard"]["checks"]["workspace"] == 0
        and row["resource_guard"]["checks"]["cache"] == 0
        for row in result.receipts
    )


def test_unreliable_resource_accounting_is_unknown_and_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(
        tmp_path, cohort_id="cohort-resource-unknown", count=1, concurrency=1
    )
    backend = _FakeBackend()

    async def broken_scan(*_args, **_kwargs):
        raise OSError("simulated accounting failure")

    monkeypatch.setattr(resource_guard_module, "scan_tree", broken_scan)

    async def scenario():
        result = await run_prepared_cohort(prepared, backend)
        leaked = [
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("pmw-resource-guard-")
            and not task.done()
        ]
        return result, leaked

    result, leaked = asyncio.run(scenario())

    assert result.outcome == "UNSAFE"
    assert result.receipts[0]["status"] == "UNKNOWN"
    assert result.receipts[0]["terminal_reason"] == (
        "RESOURCE_ACCOUNTING_UNCERTAIN"
    )
    assert backend.started == []
    assert leaked == []


def test_cancel_request_after_completion_does_not_relabel_success(
    tmp_path: Path,
) -> None:
    async def scenario() -> PreparedCohort:
        prepared = _prepared(
            tmp_path,
            cohort_id="cohort-late-cancel",
            count=1,
            concurrency=1,
        )
        backend = _FakeBackend(delay=0)
        publish_entered = asyncio.Event()
        publish_release = asyncio.Event()

        async def publish(_spec, _contribution):
            publish_entered.set()
            await publish_release.wait()
            return {"admission_ref": "admission/sha256/" + "d" * 64}

        backend.contribution = ResearchContribution(
            kind="NOTE",
            title="completed before cancellation",
            body="The host is already publishing a completed outcome.",
        )
        task = asyncio.create_task(
            run_prepared_cohort(
                prepared, backend, publisher=_Publisher(publish)
            )
        )
        await asyncio.wait_for(publish_entered.wait(), timeout=1)
        task.cancel()
        publish_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return prepared

    prepared = asyncio.run(scenario())
    settlement = RuntimeStore(prepared.cohort_root).read_settlement()
    assert settlement is not None
    assert settlement["outcome"] == "SUCCEEDED"
    assert settlement["counts"]["SUCCEEDED"] == 1
    assert settlement["counts"]["CANCELLED"] == 0
