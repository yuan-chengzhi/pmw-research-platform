"""Read-only, zero-provider tests for runtime preflight."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import pmw_platform.runtime.preflight as preflight_module
from pmw_platform.runtime.auth import PreparedCohort
from pmw_platform.runtime.context import (
    ContextWindowControl,
    ContextWindowPolicy,
)
from pmw_platform.runtime.contracts import BackendIdentity
from pmw_platform.runtime.orchestrator import RuntimeLimits
from pmw_platform.runtime.preflight import preflight_prepared_cohort
from pmw_platform.runtime.safety import load_named_profile
from pmw_platform.sessions import CohortPlan
from pmw_platform.source_lock import load_core_lock
from pmw_platform.world.records import canonical_json


SNAPSHOT = "snapshot/sha256/" + "a" * 64


def _prepared(tmp_path: Path, *, count: int = 2) -> PreparedCohort:
    profile = load_named_profile("research-default")
    core = load_core_lock()
    briefing = b'{"schema":"TEST_BRIEFING_1"}\n'
    plan = CohortPlan.generate(
        cohort_id="preflight-c01",
        world_id="math-frontier",
        world_ref="refs/pmw/math-frontier",
        base_snapshot_ref=SNAPSHOT,
        safety_profile=profile.name,
        safety_profile_sha256=profile.sha256,
        core_lock_sha256=core.sha256,
        briefing_sha256=hashlib.sha256(briefing).hexdigest(),
        count=count,
        concurrency=count,
    )
    cohort_root = tmp_path / "runs" / plan.cohort_id
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


class _Backend:
    def __init__(
        self,
        *,
        control: ContextWindowControl = ContextWindowControl.NATIVE_MODEL_WINDOW,
        verification_error: Exception | None = None,
    ) -> None:
        self.identity = BackendIdentity(
            name="preflight-test",
            protocol="TEST_RUNTIME_1",
            public_config={"source_sha256": "b" * 64},
        )
        self.context_window_control = control
        self.verification_error = verification_error
        self.verification_calls = 0
        self.start_calls = 0

    def verify_runtime(self) -> None:
        self.verification_calls += 1
        if self.verification_error is not None:
            raise self.verification_error

    async def start(self, _request):
        self.start_calls += 1
        raise AssertionError("preflight must never start a backend")


def _checks(report) -> dict[str, object]:
    return {item.name: item for item in report.checks}


def test_ready_preflight_is_canonical_bounded_and_does_not_create_runtime(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    backend = _Backend()

    report = preflight_prepared_cohort(
        prepared,
        backend,
        limits=RuntimeLimits(session_wall_seconds=None),
        context_policy=ContextWindowPolicy(default_tokens=400_000),
    )

    assert report.ready is True
    assert backend.verification_calls == 1
    assert backend.start_calls == 0
    assert not (prepared.cohort_root / "runtime").exists()
    assert not (prepared.cohort_root / ".runtime.lock").exists()
    assert report.to_bytes() == canonical_json(report.to_value()) + b"\n"
    assert len(report.to_bytes()) <= preflight_module.MAXIMUM_PREFLIGHT_REPORT_BYTES
    assert report.to_value()["preflight_sha256"] == report.sha256
    checks = _checks(report)
    assert set(checks) == {
        "plan",
        "lifecycle_limits",
        "backend_identity",
        "backend_runtime",
        "context_policy",
        "publication_identity",
        "runtime_absent",
        "runtime_claim",
        "safety_profile",
        "disk_reserve",
    }
    assert checks["backend_runtime"].code == "BACKEND_RUNTIME_PINS_VERIFIED"
    assert checks["context_policy"].evidence["configured_session_count"] == 2


@pytest.mark.parametrize(
    "policy, control",
    [
        (
            ContextWindowPolicy(session_overrides={"not-in-plan": 400_000}),
            ContextWindowControl.NATIVE_MODEL_WINDOW,
        ),
        (
            ContextWindowPolicy(default_tokens=400_000),
            ContextWindowControl.NOT_APPLICABLE,
        ),
    ],
)
def test_context_mismatch_is_a_read_only_failure(
    tmp_path: Path,
    policy: ContextWindowPolicy,
    control: ContextWindowControl,
) -> None:
    prepared = _prepared(tmp_path)
    backend = _Backend(control=control)

    report = preflight_prepared_cohort(
        prepared,
        backend,
        context_policy=policy,
    )

    check = _checks(report)["context_policy"]
    assert report.ready is False
    assert check.status == "FAIL"
    assert check.code == "CONTEXT_POLICY_UNSUPPORTED"
    if policy.session_overrides:
        assert report.context_policy_sha256 is None
    else:
        assert report.context_policy_sha256 is not None
    assert not (prepared.cohort_root / "runtime").exists()
    assert not (prepared.cohort_root / ".runtime.lock").exists()


def test_existing_runtime_and_unsafe_claim_are_reported_without_mutation(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    runtime = prepared.cohort_root / "runtime"
    runtime.mkdir()
    target = prepared.cohort_root / "unrelated.lock"
    target.write_bytes(b"unchanged")
    claim = prepared.cohort_root / ".runtime.lock"
    claim.symlink_to(target)

    report = preflight_prepared_cohort(prepared, _Backend())

    checks = _checks(report)
    assert report.ready is False
    assert checks["runtime_absent"].code == "RUNTIME_ALREADY_EXISTS"
    assert checks["runtime_claim"].code == "RUNTIME_CLAIM_UNAVAILABLE"
    assert claim.is_symlink()
    assert target.read_bytes() == b"unchanged"
    assert tuple(runtime.iterdir()) == ()


def test_existing_unheld_claim_is_checked_without_rewriting_it(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    claim = prepared.cohort_root / ".runtime.lock"
    claim.write_bytes(b"stale-but-unheld")
    before = claim.stat()

    report = preflight_prepared_cohort(prepared, _Backend())

    check = _checks(report)["runtime_claim"]
    after = claim.stat()
    assert report.ready is True
    assert check.code == "RUNTIME_CLAIM_AVAILABLE"
    assert claim.read_bytes() == b"stale-but-unheld"
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_size) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
    )
    assert not (prepared.cohort_root / "runtime").exists()


def test_disk_reserve_and_backend_pin_failures_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(tmp_path)
    backend = _Backend(
        verification_error=RuntimeError(
            "access_token=must-never-appear-in-public-report"
        )
    )
    monkeypatch.setattr(
        preflight_module.os,
        "statvfs",
        lambda _path: SimpleNamespace(
            f_frsize=1,
            f_bsize=1,
            f_blocks=10_000,
            f_bavail=0,
        ),
    )

    report = preflight_prepared_cohort(prepared, backend)

    checks = _checks(report)
    assert report.ready is False
    assert checks["backend_runtime"].code == (
        "BACKEND_RUNTIME_VERIFICATION_FAILED"
    )
    assert checks["disk_reserve"].code == "DISK_RESERVE_BREACHED"
    assert b"must-never-appear" not in report.to_bytes()


def test_backend_without_public_pin_verifier_is_not_ready(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    backend = _Backend()
    backend.verify_runtime = None  # type: ignore[assignment]

    report = preflight_prepared_cohort(prepared, backend)

    check = _checks(report)["backend_runtime"]
    assert report.ready is False
    assert check.status == "FAIL"
    assert check.code == "BACKEND_RUNTIME_VERIFICATION_FAILED"
    assert not (prepared.cohort_root / "runtime").exists()
    assert not (prepared.cohort_root / ".runtime.lock").exists()


class _Checker:
    def __init__(self, name: str, result: object) -> None:
        self.name = name
        self.result = result
        self.calls = 0

    def verify(self, **_kwargs):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_optional_local_checkers_are_named_bounded_and_affect_readiness(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    verifier = _Checker("verifier-pins", {"manifest_sha256": "c" * 64})
    broken = _Checker("tool-pins", RuntimeError("local tool drift"))

    report = preflight_prepared_cohort(
        prepared,
        _Backend(),
        checkers=(verifier, broken),
    )

    checks = _checks(report)
    assert verifier.calls == broken.calls == 1
    assert checks["hook.verifier-pins"].status == "PASS"
    assert checks["hook.tool-pins"].status == "FAIL"
    assert report.ready is False
    assert not (prepared.cohort_root / "runtime").exists()
    assert not (prepared.cohort_root / ".runtime.lock").exists()
