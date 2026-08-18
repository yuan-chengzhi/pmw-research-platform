"""Zero-model tests for the explicit runtime CLI boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

from pmw_platform import cli


def test_repeated_sigint_requests_never_escalate_past_task_cancellation() -> None:
    original = signal.getsignal(signal.SIGINT)
    observed: list[str] = []

    async def operation() -> None:
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            observed.append("first-cancel")
            handler(signal.SIGINT, None)
            try:
                await asyncio.sleep(0)
            finally:
                observed.append("cleanup-entered")
            raise

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cli._with_latched_sigint(operation()))

    assert observed == ["first-cancel", "cleanup-entered"]
    assert signal.getsignal(signal.SIGINT) is original


def _prepared(data_root: Path, cohort_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        cohort_root=data_root / "runs" / cohort_id,
        plan=SimpleNamespace(cohort_id=cohort_id),
    )


def test_runtime_data_root_preserves_symlink_for_strict_reader(
    tmp_path: Path,
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)

    selected = cli._runtime_data_root(SimpleNamespace(data_root=alias))

    assert selected == alias
    assert selected.is_symlink()


def test_session_status_is_read_only_and_uses_exact_cohort_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    emitted: list[tuple[object, object]] = []
    expected_status = {
        "launched": True,
        "settled": False,
        "terminal_sessions": 1,
        "session_count": 4,
    }

    class FakeStore:
        def __init__(self, cohort_root: Path) -> None:
            observed["cohort_root"] = cohort_root

        def read_status(self) -> dict[str, object]:
            return expected_status

    def forbidden_authentication(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("status must not authenticate or start a backend")

    monkeypatch.setattr(cli, "RuntimeStore", FakeStore)
    monkeypatch.setattr(cli, "authenticate_plan_bundle", forbidden_authentication)
    monkeypatch.setattr(
        cli,
        "_emit",
        lambda value, *, stream=cli.sys.stdout: emitted.append((value, stream)),
    )

    result = cli.main(
        [
            "--data-root",
            str(tmp_path),
            "session",
            "status",
            "--cohort",
            "cohort-01",
        ]
    )

    assert result == 0
    assert observed == {"cohort_root": tmp_path / "runs" / "cohort-01"}
    assert emitted == [
        (
            {
                "schema": "PMW_RUNTIME_STATUS_1",
                "cohort_id": "cohort-01",
                "runtime": expected_status,
            },
            cli.sys.stdout,
        )
    ]


@pytest.mark.parametrize("cohort", ["../escape", "/tmp/escape", ".", "a/b"])
def test_session_status_rejects_noncanonical_cohort_paths_before_store_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cohort: str,
) -> None:
    monkeypatch.setattr(
        cli,
        "RuntimeStore",
        lambda _root: pytest.fail("unsafe cohort reached RuntimeStore"),
    )

    result = cli.main(
        [
            "--data-root",
            str(tmp_path),
            "session",
            "status",
            "--cohort",
            cohort,
        ]
    )

    assert result == 2


@pytest.mark.parametrize(
    ("outcome", "expected_exit"),
    [("SUCCEEDED", 0), ("FAILED", 1), ("CANCELLED", 1)],
)
def test_session_start_routes_authenticated_plan_backend_and_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_exit: int,
) -> None:
    cohort_id = "cohort-02"
    config_path = tmp_path / "backend.json"
    prepared = _prepared(tmp_path, cohort_id)
    backend = object()
    observed: dict[str, object] = {}
    emitted: list[tuple[object, object]] = []

    def fake_authenticate(data_root: Path, selected_cohort: str) -> object:
        observed["authenticate"] = (data_root, selected_cohort)
        return prepared

    def fake_load_backend(path: Path) -> object:
        observed["backend_config"] = path
        return backend

    async def fake_run(
        selected_prepared: object,
        selected_backend: object,
        *,
        limits: object,
        context_policy: object,
        publisher: object,
        required_checkers: object,
        verifier_kit: object,
        agenda_arm: object,
    ) -> object:
        observed["run"] = (selected_prepared, selected_backend)
        observed["agenda_arm"] = agenda_arm
        observed["limits"] = limits
        observed["context_policy"] = context_policy
        observed["publisher"] = publisher
        observed["required_checkers"] = required_checkers
        observed["verifier_kit"] = verifier_kit
        return SimpleNamespace(
            launch_sha256="a" * 64,
            settlement_sha256="b" * 64,
            outcome=outcome,
            settlement={"counts": {outcome: 4}},
        )

    monkeypatch.setattr(cli, "authenticate_plan_bundle", fake_authenticate)
    monkeypatch.setattr(cli, "load_command_backend", fake_load_backend)
    monkeypatch.setattr(cli, "run_prepared_cohort", fake_run)
    monkeypatch.setattr(
        cli,
        "_runtime_preflight_report",
        lambda *_args: SimpleNamespace(ready=True),
    )
    monkeypatch.setattr(
        cli,
        "_emit",
        lambda value, *, stream=cli.sys.stdout: emitted.append((value, stream)),
    )

    result = cli.main(
        [
            "--data-root",
            str(tmp_path),
            "session",
            "start",
            "--cohort",
            cohort_id,
            "--backend",
            "command",
            "--backend-config",
            str(config_path),
            "--startup-seconds",
            "12.5",
            "--no-wall-limit",
            "--stop-grace-seconds",
            "3",
            "--no-verifier-kit",
        ]
    )

    assert result == expected_exit
    assert observed["authenticate"] == (tmp_path, cohort_id)
    assert observed["backend_config"] == config_path
    assert observed["run"] == (prepared, backend)
    limits = observed["limits"]
    assert limits.startup_seconds == 12.5
    assert limits.session_wall_seconds is None
    assert limits.stop_grace_seconds == 3.0
    assert observed["context_policy"].configured is False
    assert observed["publisher"] is None
    assert observed["verifier_kit"] is None
    assert observed["agenda_arm"] is None
    assert emitted == [
        (
            {
                "schema": "PMW_RUNTIME_START_RESULT_1",
                "cohort_id": cohort_id,
                "launch_sha256": "a" * 64,
                "settlement_sha256": "b" * 64,
                "outcome": outcome,
                "counts": {outcome: 4},
                "runtime_root": str(prepared.cohort_root / "runtime"),
                "verifier_kit_sha256": None,
                "agenda_arm": None,
                "agenda_arm_sha256": None,
            },
            cli.sys.stdout,
        )
    ]


def test_session_start_materializes_the_in_session_verifier_kit_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cohort_id = "cohort-kit"
    prepared = _prepared(tmp_path, cohort_id)
    prepared.briefing_bytes = b'{"schema":"TEST_BRIEFING_1"}\n'
    bindings = object()
    kit = SimpleNamespace(sha256="e" * 64)
    observed: dict[str, object] = {}
    emitted: list[object] = []

    def fake_bindings(raw: bytes) -> object:
        observed["briefing_bytes"] = raw
        return bindings

    def fake_build(
        *,
        source_materializer: object,
        target_bindings: object,
        python_executable: object,
    ) -> object:
        observed["build"] = (
            source_materializer.data_root,  # type: ignore[attr-defined]
            target_bindings,
            python_executable,
        )
        return kit

    async def fake_run(*_args: object, **kwargs: object) -> object:
        observed["verifier_kit"] = kwargs["verifier_kit"]
        return SimpleNamespace(
            launch_sha256="a" * 64,
            settlement_sha256="b" * 64,
            outcome="SUCCEEDED",
            settlement={"counts": {"SUCCEEDED": 1}},
        )

    monkeypatch.setattr(cli, "authenticate_plan_bundle", lambda *_args: prepared)
    monkeypatch.setattr(cli, "load_command_backend", lambda _path: object())
    monkeypatch.setattr(cli, "target_bindings_from_briefing", fake_bindings)
    monkeypatch.setattr(cli, "build_verifier_kit", fake_build)
    monkeypatch.setattr(cli, "run_prepared_cohort", fake_run)
    monkeypatch.setattr(
        cli,
        "_runtime_preflight_report",
        lambda *_args: SimpleNamespace(ready=True),
    )
    monkeypatch.setattr(cli, "_emit", lambda value, **_kwargs: emitted.append(value))

    result = cli.main(
        [
            "--data-root",
            str(tmp_path),
            "session",
            "start",
            "--cohort",
            cohort_id,
            "--backend",
            "command",
            "--backend-config",
            str(tmp_path / "backend.json"),
        ]
    )

    assert result == 0
    assert observed["briefing_bytes"] == prepared.briefing_bytes
    assert observed["build"] == (tmp_path, bindings, Path(cli.sys.executable).resolve())
    assert observed["verifier_kit"] is kit
    assert emitted[0]["verifier_kit_sha256"] == "e" * 64  # type: ignore[index]


def test_session_start_runtime_only_scope_ships_no_verifier_kit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(tmp_path, "cohort-runtime-only")
    observed: dict[str, object] = {}

    async def fake_run(*_args: object, **kwargs: object) -> object:
        observed["verifier_kit"] = kwargs["verifier_kit"]
        return SimpleNamespace(
            launch_sha256="a" * 64,
            settlement_sha256="b" * 64,
            outcome="SUCCEEDED",
            settlement={"counts": {"SUCCEEDED": 1}},
        )

    monkeypatch.setattr(cli, "authenticate_plan_bundle", lambda *_args: prepared)
    monkeypatch.setattr(cli, "load_command_backend", lambda _path: object())
    monkeypatch.setattr(
        cli,
        "build_verifier_kit",
        lambda **_kwargs: pytest.fail("runtime-only must not build a kit"),
    )
    monkeypatch.setattr(cli, "run_prepared_cohort", fake_run)
    monkeypatch.setattr(
        cli,
        "_runtime_preflight_report",
        lambda *_args: SimpleNamespace(ready=True),
    )
    monkeypatch.setattr(cli, "_emit", lambda _value, **_kwargs: None)

    result = cli.main(
        [
            "--data-root",
            str(tmp_path),
            "session",
            "start",
            "--cohort",
            "cohort-runtime-only",
            "--backend",
            "command",
            "--backend-config",
            str(tmp_path / "backend.json"),
            "--readiness-scope",
            "runtime-only",
        ]
    )

    assert result == 0
    assert observed["verifier_kit"] is None


def test_session_start_routes_writer_authority_only_to_host_publisher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cohort_id = "cohort-03"
    prepared = _prepared(tmp_path, cohort_id)
    backend = object()
    authority = object()
    publisher = object()
    authority_path = tmp_path / "authority.json"
    observed: dict[str, object] = {}
    emitted: list[object] = []

    monkeypatch.setattr(cli, "authenticate_plan_bundle", lambda *_args: prepared)
    monkeypatch.setattr(cli, "load_command_backend", lambda _path: backend)

    def fake_load_authority(path: Path) -> object:
        observed["authority_path"] = path
        return authority

    def fake_publisher_create(
        selected_prepared: object, selected_authority: object
    ) -> object:
        observed["publisher_create"] = (selected_prepared, selected_authority)
        return publisher

    async def fake_run(
        _prepared_value: object,
        selected_backend: object,
        *,
        limits: object,
        context_policy: object,
        publisher: object,
        required_checkers: object,
        verifier_kit: object,
        agenda_arm: object,
    ) -> object:
        observed["agenda_arm"] = agenda_arm
        observed["backend"] = selected_backend
        observed["publisher"] = publisher
        observed["context_policy"] = context_policy
        observed["required_checkers"] = required_checkers
        observed["verifier_kit"] = verifier_kit
        return SimpleNamespace(
            launch_sha256="c" * 64,
            settlement_sha256="d" * 64,
            outcome="SUCCEEDED",
            settlement={"counts": {"SUCCEEDED": 1}},
        )

    monkeypatch.setattr(cli, "load_writer_authority", fake_load_authority)
    monkeypatch.setattr(cli.PmwContributionPublisher, "create", fake_publisher_create)
    monkeypatch.setattr(cli, "run_prepared_cohort", fake_run)
    monkeypatch.setattr(
        cli,
        "_runtime_preflight_report",
        lambda *_args: SimpleNamespace(ready=True),
    )
    monkeypatch.setattr(
        cli,
        "_emit",
        lambda value, **_kwargs: emitted.append(value),
    )

    result = cli.main(
        [
            "--data-root",
            str(tmp_path),
            "session",
            "start",
            "--cohort",
            cohort_id,
            "--backend",
            "command",
            "--backend-config",
            str(tmp_path / "backend.json"),
            "--writer-authority",
            str(authority_path),
            "--no-verifier-kit",
        ]
    )

    assert result == 0
    assert observed["authority_path"] == authority_path
    assert observed["publisher_create"] == (prepared, authority)
    assert observed["publisher"] is publisher
    assert observed["backend"] is backend
    assert observed["context_policy"].configured is False
    assert len(emitted) == 1


def test_session_start_interrupt_reports_status_and_returns_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cohort_id = "cohort-04"
    prepared = _prepared(tmp_path, cohort_id)
    expected_status = {"launched": True, "settled": True, "outcome": "CANCELLED"}
    emitted: list[tuple[object, object]] = []

    class FakeStore:
        def __init__(self, cohort_root: Path) -> None:
            assert cohort_root == prepared.cohort_root

        def read_status(self) -> dict[str, object]:
            return expected_status

    async def interrupted(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "authenticate_plan_bundle", lambda *_args: prepared)
    monkeypatch.setattr(cli, "load_command_backend", lambda _path: object())
    monkeypatch.setattr(cli, "run_prepared_cohort", interrupted)
    monkeypatch.setattr(
        cli,
        "_runtime_preflight_report",
        lambda *_args: SimpleNamespace(ready=True),
    )
    monkeypatch.setattr(cli, "RuntimeStore", FakeStore)
    monkeypatch.setattr(
        cli,
        "_emit",
        lambda value, *, stream=cli.sys.stdout: emitted.append((value, stream)),
    )

    result = cli.main(
        [
            "--data-root",
            str(tmp_path),
            "session",
            "start",
            "--cohort",
            cohort_id,
            "--backend",
            "command",
            "--backend-config",
            str(tmp_path / "backend.json"),
            "--no-verifier-kit",
        ]
    )

    assert result == 130
    assert emitted == [
        (
            {
                "schema": "PMW_RUNTIME_INTERRUPTED_1",
                "cohort_id": cohort_id,
                "status": expected_status,
            },
            cli.sys.stderr,
        )
    ]


def test_post_settlement_verifier_requires_full_amf_authority_reaudit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cohort_id = "cohort-verifier"
    session_id = f"{cohort_id}-session-0001"
    workspace = tmp_path / "workspace"
    evidence = tmp_path / "evidence"
    workspace.mkdir()
    evidence.mkdir()
    prepared = SimpleNamespace(
        cohort_root=tmp_path / "runs" / cohort_id,
        briefing_bytes=b"{}\n",
        plan=SimpleNamespace(
            cohort_id=cohort_id,
            sessions=(SimpleNamespace(session_id=session_id),),
        ),
    )

    class FakeStore:
        def __init__(self, _cohort_root: Path) -> None:
            pass

        def read_settlement(self) -> object:
            return {"outcome": "SUCCEEDED"}

        def read_receipt(self, selected_session: str) -> object:
            assert selected_session == session_id
            return {"status": "SUCCEEDED"}

        def session_paths(self, selected_session: str) -> object:
            assert selected_session == session_id
            return SimpleNamespace(workspace=workspace, evidence=evidence)

    monkeypatch.setattr(cli, "authenticate_plan_bundle", lambda *_args: prepared)
    monkeypatch.setattr(cli, "RuntimeStore", FakeStore)
    monkeypatch.setattr(cli, "SourceMaterializer", lambda _root: object())
    monkeypatch.setattr(
        cli,
        "audit_amf_apparatus",
        lambda *_args: (_ for _ in ()).throw(ValueError("authority closure failed")),
    )
    monkeypatch.setattr(
        cli,
        "AmfVerifierService",
        lambda *_args, **_kwargs: pytest.fail(
            "verifier service constructed before authority re-audit"
        ),
    )

    result = cli.main(
        [
            "--data-root",
            str(tmp_path),
            "verifier",
            "run",
            "--cohort",
            cohort_id,
            "--session-id",
            session_id,
            "--target-id",
            "fixture",
            "--candidate",
            "candidate.json",
        ]
    )

    assert result == 2


@pytest.mark.parametrize("bad_value", ["0", "-1", "nan", "inf"])
def test_session_start_rejects_nonpositive_or_nonfinite_limits_before_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_value: str,
) -> None:
    emitted: list[tuple[object, object]] = []

    def forbidden_authentication(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid CLI values must fail before authentication")

    monkeypatch.setattr(cli, "authenticate_plan_bundle", forbidden_authentication)
    monkeypatch.setattr(
        cli,
        "_emit",
        lambda value, *, stream=cli.sys.stdout: emitted.append((value, stream)),
    )

    result = cli.main(
        [
            "--data-root",
            str(tmp_path),
            "session",
            "start",
            "--cohort",
            "cohort-05",
            "--backend",
            "command",
            "--backend-config",
            str(tmp_path / "backend.json"),
            "--startup-seconds",
            bad_value,
        ]
    )

    assert result == 2
    assert len(emitted) == 1
    error, stream = emitted[0]
    assert stream is cli.sys.stderr
    assert type(error) is dict
    assert error["schema"] == "PMW_RESEARCH_COMMAND_ERROR_1"
    assert error["error_type"] == "CommandLineError"
