from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

import pmw_platform.runtime.command as command_runtime
from pmw_platform.runtime.command import (
    COMMAND_BACKEND_CONFIG_SCHEMA,
    MAXIMUM_EXECUTABLE_BYTES,
    CommandBackendError,
    load_command_backend,
)
from pmw_platform.runtime.contracts import (
    BackendOutcome,
    BackendStartError,
    SessionRequest,
)
from pmw_platform.sessions import SessionSpec
from pmw_platform.world.records import canonical_json


_SHA = "1" * 64


def _request(tmp_path: Path) -> SessionRequest:
    roots: dict[str, Path] = {}
    for name in ("private_root", "workspace", "cache", "evidence"):
        selected = tmp_path / name
        selected.mkdir(mode=0o700)
        roots[name] = selected.resolve()
    briefing = tmp_path / "briefing.json"
    invocation = tmp_path / "invocation.json"
    briefing.write_bytes(b"{}")
    invocation.write_bytes(b"{}")
    spec = SessionSpec(
        session_id="cohort-session-0001",
        cohort_id="cohort",
        world_id="world",
        world_ref="refs/pmw/world",
        base_snapshot_ref=f"snapshot/sha256/{_SHA}",
        safety_profile="test-profile",
        safety_profile_sha256=_SHA,
        core_lock_sha256=_SHA,
        briefing_sha256=_SHA,
    )
    return SessionRequest(
        plan_sha256=_SHA,
        launch_sha256="2" * 64,
        spec=spec,
        briefing_path=briefing.resolve(),
        invocation_path=invocation.resolve(),
        private_root=roots["private_root"],
        workspace=roots["workspace"],
        cache=roots["cache"],
        evidence=roots["evidence"],
        session_wall_seconds=30.0,
        stop_grace_seconds=1.0,
    )


def _backend(
    tmp_path: Path,
    code: str,
    *,
    maximum_retained_bytes: int = 4_096,
    maximum_observed_bytes: int = 64 * 1024,
):
    executable = Path(sys.executable).resolve(strict=True)
    value = {
        "schema": COMMAND_BACKEND_CONFIG_SCHEMA,
        "name": "pytest-command",
        "argv": [str(executable), "-c", code],
        "argv_is_public": True,
        "result_path": "result.json",
        "capture": {
            "maximum_retained_bytes": maximum_retained_bytes,
            "maximum_observed_bytes": maximum_observed_bytes,
            "tail_bytes": min(256, maximum_retained_bytes),
        },
    }
    path = tmp_path / "command.json"
    path.write_bytes(canonical_json(value))
    return load_command_backend(path.resolve())


def _result_writer(outcome: BackendOutcome, *, prefix: str = "") -> str:
    raw = canonical_json(outcome.to_value())
    return (
        prefix
        + "from pathlib import Path\n"
        + "import os\n"
        + f"Path(os.environ['PMW_RESULT_PATH']).write_bytes({raw!r})\n"
    )


def test_command_success_has_no_host_secret_inheritance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-runtime-boundary")
    reported = BackendOutcome(
        success=True,
        terminal_reason="COMPLETED",
        summary="local success",
        usage={"steps": 1},
    )
    code = _result_writer(
        reported,
        prefix=(
            "import os\n"
            "assert 'OPENAI_API_KEY' not in os.environ\n"
            "assert set(k for k in os.environ if 'TOKEN' in k or 'KEY' in k) == set()\n"
            "print('hello from stdout')\n"
        ),
    )
    backend = _backend(tmp_path, code)
    request = _request(tmp_path)

    async def run() -> BackendOutcome:
        running = await backend.start(request)
        return await running.wait()

    outcome = asyncio.run(run())

    assert outcome.success
    assert outcome.terminal_reason == "COMPLETED"
    assert outcome.summary == "local success"
    command_evidence = outcome.evidence["command_runtime"]
    assert command_evidence["containment"] == "COOPERATIVE_PROCESS_GROUP"
    stdout = command_evidence["captures"]["stdout"]
    assert stdout["observed_sha256"] == hashlib.sha256(
        b"hello from stdout\n"
    ).hexdigest()
    assert stdout["retained_file_bytes"] == len(b"hello from stdout\n")
    assert stdout["retained_content_in_snapshot"] is False
    assert stdout["retained_storage"] == "EVIDENCE_FILE"
    assert (request.evidence / "stdout.retained.bin").read_bytes() == (
        b"hello from stdout\n"
    )
    assert "OPENAI_API_KEY" not in backend.identity.public_config[
        "environment_names"
    ]
    assert "must-not-cross-runtime-boundary" not in canonical_json(
        backend.identity.to_value()
    ).decode()


def test_nonzero_exit_is_a_job_local_backend_failure(tmp_path: Path) -> None:
    backend = _backend(
        tmp_path,
        "import sys\nsys.stderr.write('expected failure\\n')\nsys.exit(7)\n",
    )
    request = _request(tmp_path)

    async def run() -> BackendOutcome:
        return await (await backend.start(request)).wait()

    outcome = asyncio.run(run())

    assert not outcome.success
    assert outcome.terminal_reason == "COMMAND_EXIT_NONZERO"
    runtime = outcome.evidence["command_runtime"]
    assert runtime["exit_code"] == 7
    assert runtime["stop_proof"]["stopped"] is True
    assert (request.evidence / "stderr.retained.bin").read_bytes() == (
        b"expected failure\n"
    )


def test_pretty_backend_result_is_validated_then_canonicalized(tmp_path: Path) -> None:
    reported = BackendOutcome(
        success=True,
        terminal_reason="COMPLETED",
        summary="pretty JSON is not an authority boundary",
    )
    pretty = (json.dumps(reported.to_value(), indent=2, sort_keys=True) + "\n").encode()
    code = (
        "from pathlib import Path\n"
        "import os\n"
        f"Path(os.environ['PMW_RESULT_PATH']).write_bytes({pretty!r})\n"
    )
    backend = _backend(tmp_path, code)
    request = _request(tmp_path)

    async def run() -> BackendOutcome:
        return await (await backend.start(request)).wait()

    outcome = asyncio.run(run())

    assert outcome.success is True
    assert outcome.summary == reported.summary


def test_output_flood_is_drained_hashed_and_stopped_job_locally(
    tmp_path: Path,
) -> None:
    payload_bytes = 256 * 1024
    backend = _backend(
        tmp_path,
        f"import os\nos.write(1, b'x' * {payload_bytes})\n",
        maximum_retained_bytes=1_024,
        maximum_observed_bytes=4_096,
    )
    request = _request(tmp_path)

    async def run() -> BackendOutcome:
        return await (await backend.start(request)).wait()

    outcome = asyncio.run(run())

    assert not outcome.success
    assert outcome.terminal_reason == "OUTPUT_SAFETY_CAP"
    stdout = outcome.evidence["command_runtime"]["captures"]["stdout"]
    assert stdout["observed_safety_cap_exceeded"] is True
    assert stdout["truncated"] is True
    assert stdout["observed_bytes"] > 4_096
    assert len((request.evidence / "stdout.retained.bin").read_bytes()) == 1_024
    assert stdout["retained_bytes"] == 1_024
    assert stdout["retained_file_bytes"] == 1_024
    assert stdout["retained_content_in_snapshot"] is False
    # The exact byte count can be below the attempted write when SIGTERM wins,
    # but every byte actually drained is covered by the retained evidence hash.
    assert stdout["observed_sha256"] == hashlib.sha256(
        b"x" * stdout["observed_bytes"]
    ).hexdigest()


def test_stop_is_idempotent_and_proves_process_group_absent(tmp_path: Path) -> None:
    backend = _backend(tmp_path, "import time\ntime.sleep(60)\n")
    request = _request(tmp_path)

    async def run():
        running = await backend.start(request)
        await asyncio.sleep(0.05)
        first = await running.stop("TEST_STOP", 1)
        second = await running.stop("IGNORED_LATER_REASON", 1)
        outcome = await running.wait()
        return first, second, outcome

    first, second, outcome = asyncio.run(run())

    assert first == second
    assert first.stopped
    assert first.reason == "TEST_STOP"
    assert first.process_group_id is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(first.process_group_id, 0)
    assert not outcome.success
    assert outcome.terminal_reason == "TEST_STOP"


def test_leader_exit_cleans_and_rejects_residual_process_group(
    tmp_path: Path,
) -> None:
    executable = str(Path(sys.executable).resolve(strict=True))
    code = (
        "import subprocess\n"
        f"subprocess.Popen([{executable!r}, '-c', 'import time; time.sleep(60)'])\n"
    )
    backend = _backend(tmp_path, code)
    request = _request(tmp_path)

    async def run() -> BackendOutcome:
        return await (await backend.start(request)).wait()

    outcome = asyncio.run(run())

    assert not outcome.success
    assert outcome.terminal_reason == "PROCESS_GROUP_RESIDUAL"
    proof = outcome.evidence["command_runtime"]["stop_proof"]
    assert proof["stopped"] is True
    with pytest.raises(ProcessLookupError):
        os.killpg(proof["process_group_id"], 0)


def test_configuration_accepts_whitespace_and_requires_public_argv(
    tmp_path: Path,
) -> None:
    executable = Path(sys.executable).resolve(strict=True)
    value = {
        "schema": COMMAND_BACKEND_CONFIG_SCHEMA,
        "name": "pytest-command",
        "argv": [str(executable), "-c", "pass"],
        "argv_is_public": True,
        "result_path": "result.json",
        "capture": {
            "maximum_retained_bytes": 1_024,
            "maximum_observed_bytes": 2_048,
            "tail_bytes": 64,
        },
    }
    path = tmp_path / "command.json"
    path.write_bytes(canonical_json(value) + b"\n")
    loaded = load_command_backend(path.resolve())
    assert loaded.identity.public_config["argv"] == value["argv"]

    value["argv_is_public"] = False
    path.write_bytes(canonical_json(value))
    with pytest.raises(CommandBackendError, match="argv_is_public"):
        load_command_backend(path.resolve())

    value["argv_is_public"] = True
    value["result_path"] = "nested/result.json"
    path.write_bytes(canonical_json(value))
    with pytest.raises(CommandBackendError, match="result_path"):
        load_command_backend(path.resolve())


def test_executable_replacement_after_identity_hash_is_rejected(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "worker.sh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    value = {
        "schema": COMMAND_BACKEND_CONFIG_SCHEMA,
        "name": "pytest-command-drift",
        "argv": [str(executable.resolve())],
        "argv_is_public": True,
        "result_path": "result.json",
        "capture": {
            "maximum_retained_bytes": 1_024,
            "maximum_observed_bytes": 2_048,
            "tail_bytes": 64,
        },
    }
    config_path = tmp_path / "drift-command.json"
    config_path.write_bytes(canonical_json(value))
    backend = load_command_backend(config_path.resolve())
    replacement = tmp_path / "replacement.sh"
    replacement.write_text("#!/bin/sh\nexit 9\n")
    replacement.chmod(0o700)
    replacement.replace(executable)
    request_root = tmp_path / "request"
    request_root.mkdir()
    request = _request(request_root)

    async def start() -> None:
        with pytest.raises(Exception) as caught:
            await backend.start(request)
        assert getattr(caught.value, "code", None) == "COMMAND_START_FAILED"
        assert caught.value.stop_proof.stopped is True

    asyncio.run(start())


def test_oversized_executable_is_rejected_before_hashing(tmp_path: Path) -> None:
    executable = tmp_path / "oversized-worker"
    with executable.open("wb") as selected:
        selected.truncate(MAXIMUM_EXECUTABLE_BYTES + 1)
    executable.chmod(0o700)
    value = {
        "schema": COMMAND_BACKEND_CONFIG_SCHEMA,
        "name": "pytest-command-oversized",
        "argv": [str(executable.resolve())],
        "argv_is_public": True,
        "result_path": "result.json",
        "capture": {
            "maximum_retained_bytes": 1_024,
            "maximum_observed_bytes": 2_048,
            "tail_bytes": 64,
        },
    }
    config_path = tmp_path / "oversized-command.json"
    config_path.write_bytes(canonical_json(value))

    with pytest.raises(CommandBackendError, match="file is too large"):
        load_command_backend(config_path.resolve())


def test_cancelling_materialization_removes_partial_private_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path, "pass")
    request_root = tmp_path / "request"
    request_root.mkdir()
    request = _request(request_root)
    copy_yielded = asyncio.Event()

    async def pause_copy(_delay: float) -> None:
        copy_yielded.set()
        await asyncio.Future()

    monkeypatch.setattr(command_runtime.asyncio, "sleep", pause_copy)

    async def start_then_cancel() -> BackendStartError:
        start_task = asyncio.create_task(backend.start(request))
        await copy_yielded.wait()
        start_task.cancel()
        with pytest.raises(BackendStartError) as caught:
            await start_task
        return caught.value

    error = asyncio.run(start_then_cancel())

    assert error.code == "COMMAND_START_CANCELLED"
    assert error.stop_proof.stopped
    assert not (request.private_root / "command-executable").exists()
