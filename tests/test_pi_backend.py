from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import threading

import pytest

import pmw_platform.runtime.pi as pi_runtime
from pmw_platform.runtime.contracts import BackendStartError, SessionRequest
from pmw_platform.runtime.pi import (
    PI_BACKEND_CONFIG_SCHEMA,
    PiBackend,
    PiBackendConfig,
    PiBackendError,
    load_pi_backend_config,
)
from pmw_platform.sessions import SessionSpec
from pmw_platform.world.records import canonical_json


def _write(path: Path, raw: bytes, *, mode: int = 0o600) -> Path:
    path.write_bytes(raw)
    path.chmod(mode)
    return path


def _fake_node_source(mode: str) -> bytes:
    return f'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

MODE = {mode!r}

def selected(flag):
    index = sys.argv.index(flag)
    return sys.argv[index + 1]

provider = selected("--provider")
model = selected("--model")
thinking = selected("--thinking")
command_log = Path.cwd() / "rpc-command-types.jsonl"
(Path.cwd() / "fake-pi.pid").write_text(str(os.getpid()), encoding="ascii")

def emit(value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
    ending = b"\\r\\n" if MODE == "crlf" else b"\\n"
    sys.stdout.buffer.write(raw + ending)
    sys.stdout.buffer.flush()

def response(command, request_id, data=None, success=True):
    if MODE == "mismatch" and command == "get_state":
        request_id = "wrong-id"
    value = {{"type": "response", "command": command, "id": request_id, "success": success}}
    if data is not None:
        value["data"] = data
    emit(value)

for raw in sys.stdin.buffer:
    request = json.loads(raw)
    with command_log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({{"type": request["type"]}}, separators=(",", ":")) + "\\n")
    kind = request["type"]
    request_id = request["id"]
    if kind == "get_state":
        response(kind, request_id, {{
            "model": {{
                "provider": provider,
                "id": model,
                "contextWindow": 1000000,
            }},
            "thinkingLevel": thinking,
            "isStreaming": False,
            "isCompacting": False,
            "sessionId": "fake-pi-session",
        }})
    elif kind == "prompt":
        response(kind, request_id)
        if MODE != "hang":
            outcome = {{
                "schema": "PMW_RUNTIME_BACKEND_OUTCOME_1",
                "success": True,
                "terminal_reason": "RESEARCH_COMPLETED",
                "summary": "fake research completed",
                "usage": {{}},
                "evidence": {{}},
                "contributions": [],
            }}
            encoded = json.dumps(outcome, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
            text = "PMW_BACKEND_OUTCOME_JSON_BEGIN\\n" + encoded + "\\nPMW_BACKEND_OUTCOME_JSON_END"
            emit({{
                "type": "message_end",
                "message": {{
                    "role": "assistant",
                    "content": [{{"type": "text", "text": text}}],
                    "provider": provider,
                    "model": model,
                    "stopReason": "stop",
                    "usage": {{"input": 123, "output": 45}},
                }},
            }})
            emit({{"type": "agent_settled"}})
    elif kind == "get_session_stats":
        response(kind, request_id, {{
            "sessionId": "fake-pi-session",
            "tokens": {{"input": 123, "output": 45, "total": 168}},
            "contextUsage": {{"tokens": 123, "contextWindow": 1000000, "percent": 1}},
        }})
    elif kind == "abort":
        response(kind, request_id)
'''.encode()


def _runtime_fixture(tmp_path: Path, *, mode: str = "success") -> tuple[Path, Path]:
    node = _write(
        tmp_path / "fake-node",
        _fake_node_source(mode),
        mode=0o700,
    )
    installation = tmp_path / "pi-install"
    dist = installation / "dist"
    dist.mkdir(parents=True)
    entrypoint = _write(dist / "cli.js", b"// fake pinned Pi entrypoint\n")
    _write(installation / "package.json", b'{"name":"fake-pi"}\n')
    bin_root = installation / "bin"
    bin_root.mkdir()
    (bin_root / "pi").symlink_to("../dist/cli.js")

    agent_dir = tmp_path / "pi-agent-private"
    agent_dir.mkdir(mode=0o700)
    agent_dir.chmod(0o700)
    _write(
        agent_dir / "auth.json",
        canonical_json(
            {
                "fake-provider": {
                    "type": "oauth",
                    "access": "do-not-persist-this-token",
                    "refresh": "or-this-refresh-token",
                }
            }
        ),
    )
    config = {
        "schema": PI_BACKEND_CONFIG_SCHEMA,
        "name": "fake-pi",
        "node_path": str(node),
        "pi_entrypoint": str(entrypoint),
        "pi_agent_dir": str(agent_dir),
        "provider": "fake-provider",
        "model": "fake-model-1m",
        "thinking": "max",
        "auth_kind": "oauth",
        "account_label": "private-account-label",
        "tools": [],
        "extensions": [],
        "result_path": "pi-result.json",
        "limits": {
            "maximum_prompt_bytes": 1_048_576,
            "maximum_result_bytes": 1_048_576,
            "maximum_jsonl_line_bytes": 1_048_576,
            "maximum_stdout_bytes": 8_388_608,
            "maximum_retained_frame_bytes": 1_048_576,
            "maximum_stderr_bytes": 1_048_576,
            "maximum_retained_stderr_bytes": 65_536,
            "maximum_frame_count": 10_000,
            "response_timeout_seconds": 3,
        },
    }
    config_path = _write(tmp_path / "pi-backend.json", canonical_json(config))
    return config_path, agent_dir


def _request(tmp_path: Path) -> SessionRequest:
    root = tmp_path / "session-layout"
    input_root = root / "input"
    private = root / "private"
    workspace = root / "workspace"
    cache = root / "cache"
    evidence = root / "evidence"
    for path in (input_root, private, workspace, cache, evidence):
        path.mkdir(parents=True, mode=0o700)
        path.chmod(0o700)
    briefing = _write(
        input_root / "briefing.json",
        canonical_json({"schema": "TEST_BRIEFING", "problems": ["P01"]}),
    )
    invocation = _write(
        input_root / "invocation.json",
        canonical_json({"schema": "TEST_INVOCATION", "session_id": "test-s1"}),
    )
    spec = SessionSpec(
        session_id="test-s1",
        cohort_id="test-cohort",
        world_id="test-world",
        world_ref="refs/pmw/test-world",
        base_snapshot_ref="snapshot/sha256/" + "a" * 64,
        safety_profile="research",
        safety_profile_sha256="b" * 64,
        core_lock_sha256="c" * 64,
        briefing_sha256="d" * 64,
    )
    return SessionRequest(
        plan_sha256="e" * 64,
        launch_sha256="f" * 64,
        spec=spec,
        briefing_path=briefing.resolve(),
        invocation_path=invocation.resolve(),
        private_root=private.resolve(),
        workspace=workspace.resolve(),
        cache=cache.resolve(),
        evidence=evidence.resolve(),
        session_wall_seconds=10.0,
        stop_grace_seconds=1.0,
    )


def _command_types(request: SessionRequest) -> list[str]:
    path = request.workspace / "rpc-command-types.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["type"] for line in path.read_text().splitlines()]


async def _wait_fake_pid(request: SessionRequest) -> int:
    path = request.workspace / "fake-pi.pid"
    deadline = asyncio.get_running_loop().time() + 3
    while True:
        try:
            raw = path.read_text()
            if raw:
                return int(raw)
        except (FileNotFoundError, ValueError):
            pass
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("fake Pi did not publish a complete pid")
        await asyncio.sleep(0.01)


def test_config_is_strict_json_and_public_identity_redacts_oauth(tmp_path: Path) -> None:
    config_path, agent_dir = _runtime_fixture(tmp_path)
    config = load_pi_backend_config(config_path)
    backend = PiBackend(config)
    public = canonical_json(backend.identity.to_value()).decode()

    assert backend.identity.public_config["auth_kind"] == "oauth"
    assert backend.identity.public_config["provider"] == "fake-provider"
    assert len(backend.identity.public_config["account_label_sha256"]) == 64
    assert str(agent_dir) not in public
    assert "private-account-label" not in public
    assert "do-not-persist-this-token" not in public
    assert "or-this-refresh-token" not in public
    assert len(backend.identity.public_config["pi_installation_tree_sha256"]) == 64
    assert backend.identity.public_config["pi_installation_tree_protocol"] == (
        "PMW_PI_INSTALLATION_TREE_2"
    )
    assert backend.identity.public_config["runtime_pin_limits"] == {
        "maximum_node_executable_bytes": 512 * 1024 * 1024,
        "maximum_pi_entrypoint_bytes": 128 * 1024 * 1024,
        "maximum_pi_config_file_bytes": 64 * 1024 * 1024,
        "maximum_pi_extension_bytes": 64 * 1024 * 1024,
        "maximum_pi_installation_file_bytes": 512 * 1024 * 1024,
        "maximum_pi_installation_bytes": 512 * 1024 * 1024,
        "maximum_pi_installation_entries": 50_000,
    }

    # Whitespace is not an authority boundary: the validated launch identity
    # is canonicalized by the host after parsing.
    config_path.write_bytes(config_path.read_bytes() + b"\n")
    reparsed = load_pi_backend_config(config_path)
    assert reparsed.to_public_value() == config.to_public_value()


def test_pi_identity_covers_the_complete_installation_tree(tmp_path: Path) -> None:
    config_path, _agent_dir = _runtime_fixture(tmp_path)
    config = load_pi_backend_config(config_path)
    package_file = config.pi_entrypoint.parent.parent / "package.json"
    package_file.write_text('{"version":"drifted"}\n', encoding="utf-8")

    with pytest.raises(PiBackendError) as raised:
        config.verify_runtime()

    assert raised.value.code == "PI_RUNTIME_FILE_DRIFT"


def test_installation_tree_digest_binds_empty_directories(tmp_path: Path) -> None:
    root = (tmp_path / "installation").resolve()
    root.mkdir()
    _write(root / "package.json", b"{}\n")
    before = pi_runtime._tree_digest(root)

    (root / "empty-directory").mkdir()
    after = pi_runtime._tree_digest(root)

    assert before != after


def test_installation_entry_limit_counts_empty_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (tmp_path / "installation").resolve()
    root.mkdir()
    for name in ("one", "two", "three"):
        (root / name).mkdir()
    monkeypatch.setattr(pi_runtime, "MAXIMUM_PI_INSTALLATION_ENTRIES", 2)

    with pytest.raises(PiBackendError) as raised:
        pi_runtime._tree_digest(root)

    assert raised.value.code == "PI_INSTALLATION_TREE_INVALID"
    assert raised.value.detail == "entry limit"


def test_installation_byte_limit_is_checked_before_any_file_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (tmp_path / "installation").resolve()
    root.mkdir()
    _write(root / "oversized", b"12")
    monkeypatch.setattr(pi_runtime, "MAXIMUM_PI_INSTALLATION_BYTES", 1)

    def unexpected_open(_path: Path) -> int:
        raise AssertionError("oversized tree file was opened for hashing")

    monkeypatch.setattr(pi_runtime, "_open_readonly_nofollow", unexpected_open)
    with pytest.raises(PiBackendError) as raised:
        pi_runtime._tree_digest(root)

    assert raised.value.code == "PI_INSTALLATION_TREE_INVALID"
    assert raised.value.detail == "byte limit"


def test_file_pin_rejects_oversize_before_opening_for_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write(tmp_path / "oversized", b"12").resolve()

    def unexpected_open(_path: Path) -> int:
        raise AssertionError("oversized pin was opened for hashing")

    monkeypatch.setattr(pi_runtime, "_open_readonly_nofollow", unexpected_open)
    with pytest.raises(PiBackendError) as raised:
        pi_runtime._FilePin.create(path, maximum_bytes=1)

    assert raised.value.code == "PI_RUNTIME_FILE_INVALID"
    assert raised.value.detail.endswith("byte limit")


def test_file_pin_detects_growth_during_bounded_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write(tmp_path / "moving", b"stable").resolve()
    original_read = os.read
    mutated = False

    def growing_read(descriptor: int, maximum: int) -> bytes:
        nonlocal mutated
        chunk = original_read(descriptor, maximum)
        if chunk and not mutated:
            mutated = True
            with path.open("ab") as stream:
                stream.write(b"+")
        return chunk

    monkeypatch.setattr(pi_runtime.os, "read", growing_read)
    with pytest.raises(PiBackendError) as raised:
        pi_runtime._FilePin.create(path, maximum_bytes=64)

    assert raised.value.code == "PI_RUNTIME_FILE_DRIFT"
    assert mutated is True


def test_installation_tree_rechecks_nested_directory_after_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (tmp_path / "installation").resolve()
    nested = root / "nested"
    nested.mkdir(parents=True)
    _write(nested / "payload", b"stable")
    original_create = pi_runtime._FilePin.create.__func__
    mutated = False

    def create_and_mutate(cls, path: Path, **kwargs):
        nonlocal mutated
        pin = original_create(cls, path, **kwargs)
        if path.name == "payload" and not mutated:
            mutated = True
            (nested / "late-empty-directory").mkdir()
        return pin

    monkeypatch.setattr(
        pi_runtime._FilePin,
        "create",
        classmethod(create_and_mutate),
    )
    with pytest.raises(PiBackendError) as raised:
        pi_runtime._tree_digest(root)

    assert raised.value.code == "PI_INSTALLATION_TREE_INVALID"
    assert raised.value.detail == "directory drift: nested"
    assert mutated is True


def test_oauth_boundary_rejects_permissions_and_non_oauth_kind(tmp_path: Path) -> None:
    config_path, agent_dir = _runtime_fixture(tmp_path)
    auth_path = agent_dir / "auth.json"
    auth_path.chmod(0o640)
    with pytest.raises(PiBackendError) as raised:
        load_pi_backend_config(config_path)
    assert raised.value.code == "PI_AUTH_BOUNDARY_INVALID"

    auth_path.chmod(0o600)
    auth_path.write_bytes(
        canonical_json({"fake-provider": {"type": "api_key", "key": "secret"}})
    )
    with pytest.raises(PiBackendError) as raised:
        load_pi_backend_config(config_path)
    assert raised.value.code == "PI_AUTH_KIND_MISMATCH"


def test_fake_rpc_success_records_official_context_without_host_cap(
    tmp_path: Path,
) -> None:
    config_path, _agent_dir = _runtime_fixture(tmp_path)
    backend = PiBackend(load_pi_backend_config(config_path))
    request = _request(tmp_path)

    async def run():
        handle = await backend.start(request)
        return await handle.wait()

    outcome = asyncio.run(run())
    assert outcome.success is True
    assert outcome.terminal_reason == "RESEARCH_COMPLETED"
    assert outcome.usage["pi_rpc"]["pi_reported_context_window"] == 1_000_000
    assert outcome.usage["pi_rpc"]["account_route_context_acceptance"] == (
        "NOT_MEASURED_BY_ADAPTER"
    )
    runtime = outcome.evidence["pi_rpc"]
    assert runtime["pi_reported_context_window"] == 1_000_000
    assert runtime["account_route_context_acceptance"] == "NOT_MEASURED_BY_ADAPTER"
    assert runtime["host_context_limit"] is None
    assert runtime["host_prompt_count"] == 1
    assert runtime["host_retry_count"] == 0
    assert runtime["host_compaction_count"] == 0
    assert runtime["result_source"] == "final_message_envelope"
    assert runtime["single_stdout_reader"] is True
    assert runtime["strict_lf_jsonl"] is True
    assert runtime["stop_proof"]["stopped"] is True
    assert _command_types(request) == [
        "get_state",
        "prompt",
        "get_state",
        "get_session_stats",
    ]
    frames_path = request.evidence / "pi.frames.jsonl"
    assert frames_path.stat().st_size <= 1_048_576
    assert b"do-not-persist-this-token" not in frames_path.read_bytes()


@pytest.mark.parametrize(
    ("mode", "reason"),
    [("crlf", "RPC_MALFORMED_FRAME"), ("mismatch", "RPC_RESPONSE_MISMATCH")],
)
def test_rpc_framing_and_response_identity_fail_closed(
    tmp_path: Path,
    mode: str,
    reason: str,
) -> None:
    config_path, _agent_dir = _runtime_fixture(tmp_path, mode=mode)
    backend = PiBackend(load_pi_backend_config(config_path))
    request = _request(tmp_path)

    async def run():
        handle = await backend.start(request)
        return await handle.wait()

    outcome = asyncio.run(run())
    assert outcome.success is False
    assert outcome.terminal_reason == reason
    assert outcome.evidence["pi_rpc"]["stop_proof"]["stopped"] is True


def test_stop_sends_abort_then_proves_process_group_absent(tmp_path: Path) -> None:
    config_path, _agent_dir = _runtime_fixture(tmp_path, mode="hang")
    backend = PiBackend(load_pi_backend_config(config_path))
    request = _request(tmp_path)

    async def run():
        handle = await backend.start(request)
        waiter = asyncio.create_task(handle.wait())
        deadline = asyncio.get_running_loop().time() + 3
        while "prompt" not in _command_types(request):
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("fake Pi never received the prompt")
            await asyncio.sleep(0.01)
        proof = await handle.stop("TEST_CANCEL", 0.5)
        repeated = await handle.stop("TEST_CANCEL", 0.5)
        outcome = await waiter
        return proof, repeated, outcome

    proof, repeated, outcome = asyncio.run(run())
    assert proof.stopped is True
    assert proof.reason == "TEST_CANCEL"
    assert repeated == proof
    assert outcome.success is False
    assert "abort" in _command_types(request)


def test_stop_joins_completion_even_when_provider_emits_no_more_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _agent_dir = _runtime_fixture(tmp_path, mode="hang")
    backend = PiBackend(load_pi_backend_config(config_path))
    request = _request(tmp_path)

    async def silent_events(self, *, timeout):
        del self, timeout
        await asyncio.Future()

    monkeypatch.setattr(
        pi_runtime._PiRpcTransport, "next_event", silent_events
    )

    async def run():
        handle = await backend.start(request)
        deadline = asyncio.get_running_loop().time() + 3
        while "prompt" not in _command_types(request):
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("fake Pi never received the prompt")
            await asyncio.sleep(0.01)
        proof = await handle.stop("TEST_CANCEL", 0.5)
        outcome = await handle.wait()
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name().endswith(":pi-session")
        ]
        return handle, proof, outcome, pending

    handle, proof, outcome, pending = asyncio.run(run())
    assert proof.stopped is True
    assert handle.completion.done()
    assert outcome.success is False
    assert outcome.terminal_reason == "STOP_REQUESTED"
    assert pending == []


def test_linux_kernel_ps_rows_do_not_break_descendant_discovery() -> None:
    snapshot = b"""\
2 0 0
100 1 100
101 100 100
102 101 102
"""

    assert pi_runtime._descendant_groups_from_ps(
        snapshot,
        root_pid=100,
        root_group=100,
    ) == (102,)

    with pytest.raises(pi_runtime.PiRpcFailure) as raised:
        pi_runtime._descendant_groups_from_ps(
            b"100 1 100\n103 100 0\n",
            root_pid=100,
            root_group=100,
        )
    assert raised.value.code == "PROCESS_GROUP_IDENTITY_FAILURE"


def test_descendant_discovery_failure_can_never_claim_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _agent_dir = _runtime_fixture(tmp_path, mode="hang")
    backend = PiBackend(load_pi_backend_config(config_path))
    request = _request(tmp_path)

    async def unavailable(*_args, **_kwargs):
        raise pi_runtime.PiRpcFailure("PROCESS_GROUP_DISCOVERY_FAILED")

    async def run():
        handle = await backend.start(request)
        waiter = asyncio.create_task(handle.wait())
        deadline = asyncio.get_running_loop().time() + 3
        while "prompt" not in _command_types(request):
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("fake Pi never received the prompt")
            await asyncio.sleep(0.01)
        monkeypatch.setattr(pi_runtime, "_discover_descendant_groups", unavailable)
        proof = await handle.stop("TEST_CANCEL", 0.5)
        outcome = await waiter
        return proof, outcome

    proof, outcome = asyncio.run(run())
    assert proof.stopped is False
    assert "unproven" in proof.detail
    assert outcome.success is False
    runtime = outcome.evidence["pi_rpc"]
    assert runtime["descendant_discovery_failure"] == (
        "PROCESS_GROUP_DISCOVERY_FAILED"
    )


def test_cancelled_stop_caller_cannot_cancel_terminal_cleanup(tmp_path: Path) -> None:
    config_path, _agent_dir = _runtime_fixture(tmp_path, mode="hang")
    backend = PiBackend(load_pi_backend_config(config_path))
    request = _request(tmp_path)

    async def run():
        handle = await backend.start(request)
        waiter = asyncio.create_task(handle.wait())
        deadline = asyncio.get_running_loop().time() + 3
        while "prompt" not in _command_types(request):
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("fake Pi never received the prompt")
            await asyncio.sleep(0.01)
        first = asyncio.create_task(handle.stop("TEST_CANCEL", 0.5))
        while handle.transport.shutdown_task is None:
            await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        proof = await handle.stop("TEST_CANCEL", 0.5)
        outcome = await waiter
        return handle, proof, outcome

    handle, proof, outcome = asyncio.run(run())
    assert proof.stopped is True
    assert handle.transport.frames.closed is True
    assert handle.transport.stderr.closed is True
    assert handle.transport.stop_proof == proof
    assert outcome.success is False


def test_post_spawn_verifier_cancellation_cleans_process_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _agent_dir = _runtime_fixture(tmp_path, mode="hang")
    backend = PiBackend(load_pi_backend_config(config_path))
    request = _request(tmp_path)
    original = PiBackendConfig.verify_runtime
    entered = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    calls = 0

    def blocked_second_verification(self: PiBackendConfig) -> None:
        nonlocal calls
        with counter_lock:
            calls += 1
            ordinal = calls
        if ordinal == 2:
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release post-spawn verifier")
        original(self)

    monkeypatch.setattr(
        PiBackendConfig, "verify_runtime", blocked_second_verification
    )

    async def run():
        start = asyncio.create_task(backend.start(request))
        assert await asyncio.to_thread(entered.wait, 3)
        pid = await _wait_fake_pid(request)
        start.cancel()
        await asyncio.sleep(0.05)
        assert not start.done(), "start returned before verifier/cleanup joined"
        release.set()
        with pytest.raises(BackendStartError) as raised:
            await start
        await asyncio.sleep(0)
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        return raised.value, pid, pending

    error, pid, pending = asyncio.run(run())
    assert error.code == "PI_START_CANCELLED"
    assert error.stop_proof is not None and error.stop_proof.stopped is True
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert pending == []


def test_cancel_during_start_error_cleanup_still_joins_stop_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _agent_dir = _runtime_fixture(tmp_path, mode="hang")
    backend = PiBackend(load_pi_backend_config(config_path))
    request = _request(tmp_path)
    original = PiBackendConfig.verify_runtime
    calls = 0
    cleanup_entered: asyncio.Event
    cleanup_release: asyncio.Event

    def failing_second_verification(self: PiBackendConfig) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PiBackendError("PI_RUNTIME_FILE_DRIFT", "injected")
        original(self)

    monkeypatch.setattr(
        PiBackendConfig, "verify_runtime", failing_second_verification
    )

    async def run():
        nonlocal cleanup_entered, cleanup_release
        cleanup_entered = asyncio.Event()
        cleanup_release = asyncio.Event()

        async def blocked_discovery(*_args, **_kwargs):
            cleanup_entered.set()
            await cleanup_release.wait()
            return ()

        monkeypatch.setattr(
            pi_runtime, "_discover_descendant_groups", blocked_discovery
        )
        start = asyncio.create_task(backend.start(request))
        await asyncio.wait_for(cleanup_entered.wait(), timeout=3)
        pid = await _wait_fake_pid(request)
        start.cancel()
        await asyncio.sleep(0.05)
        assert not start.done(), "cancellation escaped active cleanup"
        cleanup_release.set()
        with pytest.raises(BackendStartError) as raised:
            await start
        await asyncio.sleep(0)
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        return raised.value, pid, pending

    error, pid, pending = asyncio.run(run())
    assert error.code == "PI_START_CANCELLED"
    assert error.stop_proof is not None and error.stop_proof.stopped is True
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert pending == []


def test_evidence_close_failure_cannot_return_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _agent_dir = _runtime_fixture(tmp_path)
    backend = PiBackend(load_pi_backend_config(config_path))
    request = _request(tmp_path)
    original = pi_runtime._BoundedEvidence.close

    def failed_close(self):
        original(self)
        self.write_failed = True

    monkeypatch.setattr(pi_runtime._BoundedEvidence, "close", failed_close)

    async def run():
        handle = await backend.start(request)
        return handle, await handle.wait()

    handle, outcome = asyncio.run(run())
    assert outcome.success is False
    assert outcome.terminal_reason == "RPC_EVIDENCE_WRITE_FAILED"
    assert outcome.evidence["pi_rpc"]["stop_proof"]["stopped"] is True
    assert handle.transport.frames.closed is True
    assert handle.transport.evidence_write_failed is True
