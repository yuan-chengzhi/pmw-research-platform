"""Zero-model tests for measured usage in session receipts.

No test here contacts a provider.  A fake Pi RPC child emits exactly the
frames the pinned Pi tree documents, and the assertions are about what the
durable receipt says those frames measured -- or honestly failed to measure.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from pmw_platform.runtime.auth import PreparedCohort
from pmw_platform.runtime.command import (
    COMMAND_BACKEND_CONFIG_SCHEMA,
    load_command_backend,
)
from pmw_platform.runtime.contracts import BackendOutcome, RuntimeContractError
from pmw_platform.runtime.orchestrator import RuntimeLimits, run_prepared_cohort
from pmw_platform.runtime.pi import (
    PI_BACKEND_CONFIG_SCHEMA,
    PiBackend,
    load_pi_backend_config,
)
from pmw_platform.runtime.safety import load_named_profile
from pmw_platform.runtime.usage import (
    USAGE_EVIDENCE_SCHEMA,
    UsageEvidence,
    UsageEvidenceError,
    UsageRequestRecord,
    UsageState,
    UsageTotals,
    summed_totals,
)
from pmw_platform.sessions import CohortPlan
from pmw_platform.source_lock import load_core_lock
from pmw_platform.world.records import canonical_json


SNAPSHOT = "snapshot/sha256/" + "a" * 64

# The exact usage objects the fake Pi child reports.  Every expected number in
# this module is derived from these four readings and nothing else.
_TOOL_TURN_USAGE = {
    "input": 1_000,
    "cacheRead": 24_000,
    "cacheWrite": 512,
    "output": 12,
    "totalTokens": 25_524,
    "cost": {
        "input": 0.003,
        "output": 0.00018,
        "cacheRead": 0.0072,
        "cacheWrite": 0.00192,
        "total": 0.0123,
    },
}
_TOOL_RESULT_USAGE = {"input": 5, "output": 6, "totalTokens": 11}
_COMPACTION_USAGE = {
    "input": 32_000,
    "cacheRead": 0,
    "cacheWrite": 0,
    "output": 1_200,
    "totalTokens": 33_200,
}
_FINAL_TURN_USAGE = {
    "input": 2_000,
    "cacheRead": 480_000,
    "cacheWrite": 0,
    "output": 300,
    "reasoning": 120,
    "totalTokens": 482_420,
}
_SESSION_TOKENS = {
    "input": 35_005,
    "output": 1_518,
    "cacheRead": 504_000,
    "cacheWrite": 512,
    "total": 541_155,
}
_CONTEXT_USAGE = {
    "tokens": 482_420,
    "contextWindow": 1_000_000,
    "percent": 48.2,
}


def _write(path: Path, raw: bytes, *, mode: int = 0o600) -> Path:
    path.write_bytes(raw)
    path.chmod(mode)
    return path


def _fake_pi_source(*, reports_usage: bool) -> bytes:
    """A fake Pi child that speaks the documented RPC surface.

    ``reports_usage`` toggles the one thing under test: whether the surface
    answers with token numbers at all.  A silent surface is a real Pi
    possibility (an older build, a provider that reports nothing), and the
    platform must say ``UNMEASURED`` rather than invent a zero.
    """

    session_stats: dict[str, object] = {
        "sessionFile": "/fake/session.jsonl",
        "sessionId": "fake-pi-session",
        "userMessages": 1,
        "assistantMessages": 2,
        "toolCalls": 1,
        "toolResults": 1,
        "totalMessages": 5,
    }
    if reports_usage:
        session_stats["tokens"] = _SESSION_TOKENS
        session_stats["cost"] = 0.45
        session_stats["contextUsage"] = _CONTEXT_USAGE
    turns: list[dict[str, object]] = [
        {
            "event": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "provider": "fake-provider",
                "model": "fake-model-1m",
                "stopReason": "toolUse",
                "usage": _TOOL_TURN_USAGE,
            },
        },
        {
            "event": "message_end",
            "message": {
                "role": "toolResult",
                "toolCallId": "call_1",
                "toolName": "bash",
                "content": [{"type": "text", "text": "ok"}],
                "usage": _TOOL_RESULT_USAGE,
            },
        },
        {
            "event": "compaction_end",
            "reason": "threshold",
            "aborted": False,
            "willRetry": False,
            "result": {
                "summary": "compacted",
                "firstKeptEntryId": "entry-1",
                "tokensBefore": 900_000,
                "estimatedTokensAfter": 32_000,
                "usage": _COMPACTION_USAGE,
                "details": {},
            },
        },
    ]
    if not reports_usage:
        for turn in turns:
            message = turn.get("message")
            if type(message) is dict:
                message.pop("usage", None)
            result = turn.get("result")
            if type(result) is dict:
                result.pop("usage", None)
    final_usage = json.dumps(_FINAL_TURN_USAGE if reports_usage else None)
    return f'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

TURNS = json.loads({json.dumps(json.dumps(turns))})
SESSION_STATS = json.loads({json.dumps(json.dumps(session_stats))})
FINAL_USAGE = json.loads({json.dumps(final_usage)})


def selected(flag):
    index = sys.argv.index(flag)
    return sys.argv[index + 1]


provider = selected("--provider")
model = selected("--model")
thinking = selected("--thinking")
context_window = (
    int(selected("--pmw-context-window-tokens"))
    if "--pmw-context-window-tokens" in sys.argv
    else 1000000
)


def emit(value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    sys.stdout.buffer.write(raw.encode() + b"\\n")
    sys.stdout.buffer.flush()


def response(command, request_id, data=None):
    value = {{"type": "response", "command": command, "id": request_id, "success": True}}
    if data is not None:
        value["data"] = data
    emit(value)


for raw in sys.stdin.buffer:
    request = json.loads(raw)
    kind = request["type"]
    request_id = request["id"]
    if kind == "get_state":
        response(kind, request_id, {{
            "model": {{
                "provider": provider,
                "id": model,
                "contextWindow": context_window,
            }},
            "thinkingLevel": thinking,
            "isStreaming": False,
            "isCompacting": False,
            "autoCompactionEnabled": False,
            "sessionId": "fake-pi-session",
        }})
    elif kind == "set_auto_compaction":
        response(kind, request_id)
    elif kind == "prompt":
        response(kind, request_id)
        for turn in TURNS:
            frame = dict(turn)
            frame["type"] = frame.pop("event")
            emit(frame)
        outcome = {{
            "schema": "PMW_RUNTIME_BACKEND_OUTCOME_1",
            "success": True,
            "terminal_reason": "RESEARCH_COMPLETED",
            "summary": "fake research completed",
            "usage": {{"self_reported_model_calls": 3}},
            "evidence": {{}},
            "contributions": [],
        }}
        encoded = json.dumps(outcome, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
        text = "PMW_BACKEND_OUTCOME_JSON_BEGIN\\n" + encoded + "\\nPMW_BACKEND_OUTCOME_JSON_END"
        message = {{
            "role": "assistant",
            "content": [{{"type": "text", "text": text}}],
            "provider": provider,
            "model": model,
            "stopReason": "stop",
        }}
        if FINAL_USAGE is not None:
            message["usage"] = FINAL_USAGE
        emit({{"type": "message_end", "message": message}})
        emit({{"type": "agent_settled"}})
    elif kind == "get_session_stats":
        response(kind, request_id, SESSION_STATS)
    elif kind == "abort":
        response(kind, request_id)
'''.encode()


def _pi_backend(tmp_path: Path, *, reports_usage: bool) -> PiBackend:
    node = _write(
        tmp_path / "fake-node",
        _fake_pi_source(reports_usage=reports_usage),
        mode=0o700,
    )
    installation = tmp_path / "pi-install"
    dist = installation / "dist"
    dist.mkdir(parents=True)
    entrypoint = _write(dist / "cli.js", b"// fake pinned Pi entrypoint\n")
    _write(installation / "package.json", b'{"name":"fake-pi"}\n')
    agent_dir = tmp_path / "pi-agent-private"
    agent_dir.mkdir(mode=0o700)
    agent_dir.chmod(0o700)
    _write(
        agent_dir / "auth.json",
        canonical_json({"fake-provider": {"type": "oauth", "access": "secret"}}),
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
        "expected_context_window_tokens": None,
        "disable_auto_compaction": False,
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
            "response_timeout_seconds": 10,
        },
    }
    path = _write(tmp_path / "pi-backend.json", canonical_json(config))
    return PiBackend(load_pi_backend_config(path))


def _command_backend(tmp_path: Path):
    worker = Path(__file__).resolve().parent.parent / "examples" / "model-free-worker.py"
    value = {
        "schema": COMMAND_BACKEND_CONFIG_SCHEMA,
        "name": "model-free-worker",
        "argv": [str(Path(sys.executable).resolve(strict=True)), str(worker)],
        "argv_is_public": True,
        "result_path": "result.json",
        "capture": {
            "maximum_retained_bytes": 4_096,
            "maximum_observed_bytes": 65_536,
            "tail_bytes": 256,
        },
    }
    path = _write(tmp_path / "command.json", canonical_json(value))
    return load_command_backend(path.resolve())


def _prepared(tmp_path: Path, *, cohort_id: str) -> PreparedCohort:
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
        count=1,
        concurrency=1,
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


def _single_receipt(tmp_path: Path, backend, *, cohort_id: str) -> dict[str, object]:
    prepared = _prepared(tmp_path, cohort_id=cohort_id)
    result = asyncio.run(
        run_prepared_cohort(
            prepared,
            backend,
            limits=RuntimeLimits(
                startup_seconds=30.0,
                session_wall_seconds=60.0,
                stop_grace_seconds=2.0,
            ),
        )
    )
    assert len(result.receipts) == 1
    return result.receipts[0]


def test_reported_pi_usage_reaches_the_receipt_verbatim(tmp_path: Path) -> None:
    receipt = _single_receipt(
        tmp_path,
        _pi_backend(tmp_path, reports_usage=True),
        cohort_id="usage-measured",
    )

    assert receipt["status"] == "SUCCEEDED"
    usage = receipt["usage"]
    assert usage["schema"] == USAGE_EVIDENCE_SCHEMA
    assert usage["state"] == "MEASURED"
    assert usage["provenance"] == "PI_RPC_REPORTED"
    assert usage["assertion"] is None
    assert usage["requests_truncated"] is False
    # Every reported request survives, including the tool turn and the
    # compaction that a last-message-only reading would have thrown away.
    assert usage["requests"] == [
        {
            "ordinal": 1,
            "source_event": "message_end",
            "role": "assistant",
            "provider": "fake-provider",
            "model": "fake-model-1m",
            "stop_reason": "toolUse",
            "input_tokens": 1_000,
            "cached_input_tokens": 24_000,
            "cache_write_tokens": 512,
            "output_tokens": 12,
            "reasoning_tokens": None,
            "total_tokens": 25_524,
        },
        {
            "ordinal": 2,
            "source_event": "message_end",
            "role": "toolResult",
            "provider": None,
            "model": None,
            "stop_reason": None,
            "input_tokens": 5,
            "cached_input_tokens": None,
            "cache_write_tokens": None,
            "output_tokens": 6,
            "reasoning_tokens": None,
            "total_tokens": 11,
        },
        {
            "ordinal": 3,
            "source_event": "compaction_end",
            "role": "compaction",
            "provider": None,
            "model": None,
            "stop_reason": None,
            "input_tokens": 32_000,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 1_200,
            "reasoning_tokens": None,
            "total_tokens": 33_200,
        },
        {
            "ordinal": 4,
            "source_event": "message_end",
            "role": "assistant",
            "provider": "fake-provider",
            "model": "fake-model-1m",
            "stop_reason": "stop",
            "input_tokens": 2_000,
            "cached_input_tokens": 480_000,
            "cache_write_tokens": 0,
            "output_tokens": 300,
            "reasoning_tokens": 120,
            "total_tokens": 482_420,
        },
    ]
    assert usage["totals"] == [
        {
            "basis": "HOST_SUMMED_OBSERVED_RECORDS",
            "request_count": 4,
            "input_tokens": 35_005,
            "cached_input_tokens": 504_000,
            "cache_write_tokens": 512,
            "output_tokens": 1_518,
            "reasoning_tokens": 120,
            "total_tokens": 541_155,
        },
        {
            "basis": "RUNTIME_REPORTED_SESSION_TOTALS",
            "request_count": None,
            "input_tokens": 35_005,
            "cached_input_tokens": 504_000,
            "cache_write_tokens": 512,
            "output_tokens": 1_518,
            "reasoning_tokens": None,
            "total_tokens": 541_155,
        },
    ]
    assert usage["provider_reported_context_tokens"] == 482_420
    assert usage["provider_reported_context_window_tokens"] == 1_000_000
    # The session's own free-form self-report still rides along untouched, and
    # is still not what the typed measurement is built from.
    assert receipt["outcome"]["usage"]["self_reported_model_calls"] == 3
    assert receipt["outcome"]["evidence"]["pi_rpc"]["observed_pi_usage_reports"] == 4


def test_silent_pi_usage_surface_is_unmeasured_and_never_zero(
    tmp_path: Path,
) -> None:
    receipt = _single_receipt(
        tmp_path,
        _pi_backend(tmp_path, reports_usage=False),
        cohort_id="usage-silent",
    )

    assert receipt["status"] == "SUCCEEDED"
    usage = receipt["usage"]
    assert usage["state"] == "UNMEASURED"
    assert usage["provenance"] == "PI_RPC_SURFACE_SILENT"
    assert usage["requests"] == []
    assert usage["totals"] == []
    assert usage["assertion"] is None
    assert usage["provider_reported_context_tokens"] is None
    assert usage["provider_reported_context_window_tokens"] is None
    # A silent surface must not be laundered into a measured zero anywhere in
    # the receipt's typed usage block.
    assert "0" not in json.dumps(usage)
    assert receipt["outcome"]["evidence"]["pi_rpc"]["observed_pi_usage_reports"] == 0


def test_model_free_command_backend_keeps_its_asserted_zero(
    tmp_path: Path,
) -> None:
    receipt = _single_receipt(
        tmp_path,
        _command_backend(tmp_path),
        cohort_id="usage-model-free",
    )

    assert receipt["status"] == "SUCCEEDED"
    usage = receipt["usage"]
    # The zero survives, and it is labeled an assertion of the profile rather
    # than a measurement of anything.
    assert usage["state"] == "ASSERTED"
    assert usage["provenance"] == "COMMAND_BACKEND_MODEL_FREE_PROFILE"
    assert usage["assertion"] == {
        "adapter_model_calls": 0,
        "adapter_provider_requests": 0,
    }
    assert "Not a measurement" in usage["detail"]
    assert usage["requests"] == []
    assert usage["totals"] == []
    assert usage["provider_reported_context_tokens"] is None
    assert receipt["outcome"]["usage"] == {"model_calls": 0, "network_calls": 0}


def test_a_session_result_envelope_cannot_declare_its_own_measurement() -> None:
    envelope = {
        "schema": "PMW_RUNTIME_BACKEND_OUTCOME_1",
        "success": True,
        "terminal_reason": "COMPLETED",
        "summary": "",
        "usage": {},
        "evidence": {},
        "contributions": [],
        "usage_evidence": UsageEvidence.measured(
            provenance="PI_RPC_REPORTED",
            totals=(UsageTotals(basis="FABRICATED", total_tokens=1),),
        ).to_value(),
    }

    with pytest.raises(RuntimeContractError):
        BackendOutcome.from_value(envelope)

    accepted = dict(envelope)
    accepted.pop("usage_evidence")
    outcome = BackendOutcome.from_value(accepted)
    assert outcome.usage_evidence.state is UsageState.UNMEASURED
    assert outcome.usage_evidence.provenance == (
        "BACKEND_DECLARED_NO_USAGE_EVIDENCE"
    )


def test_an_unmeasured_or_asserted_block_can_never_carry_a_reading() -> None:
    with pytest.raises(UsageEvidenceError):
        UsageEvidence(
            state=UsageState.UNMEASURED,
            provenance="PI_RPC_SURFACE_SILENT",
            totals=(UsageTotals(basis="SOMEWHERE", total_tokens=0),),
        )
    with pytest.raises(UsageEvidenceError):
        UsageEvidence(
            state=UsageState.ASSERTED,
            provenance="COMMAND_BACKEND_MODEL_FREE_PROFILE",
            assertion=(("adapter_model_calls", 0),),
            provider_reported_context_tokens=10,
        )
    with pytest.raises(UsageEvidenceError):
        UsageEvidence.measured(provenance="PI_RPC_REPORTED")
    with pytest.raises(UsageEvidenceError):
        UsageEvidence(
            state=UsageState.MEASURED,
            provenance="PI_RPC_REPORTED",
            requests=(
                UsageRequestRecord(
                    ordinal=1,
                    source_event="message_end",
                    role="assistant",
                    input_tokens=1,
                ),
            ),
            assertion=(("adapter_model_calls", 0),),
        )


def test_summing_never_turns_an_unreported_field_into_zero() -> None:
    totals = summed_totals(
        [
            UsageRequestRecord(
                ordinal=1,
                source_event="message_end",
                role="assistant",
                input_tokens=10,
                output_tokens=2,
            ),
            UsageRequestRecord(
                ordinal=2,
                source_event="message_end",
                role="assistant",
                input_tokens=5,
                output_tokens=3,
                cached_input_tokens=7,
            ),
        ]
    )

    assert totals.request_count == 2
    assert totals.input_tokens == 15
    assert totals.output_tokens == 5
    assert totals.cached_input_tokens == 7
    # No record reported these, so neither does the aggregate.
    assert totals.cache_write_tokens is None
    assert totals.reasoning_tokens is None
    assert totals.total_tokens is None


def test_a_runtime_that_answers_with_junk_has_still_measured_nothing() -> None:
    from pmw_platform.runtime.pi import _PiUsageCollector

    collector = _PiUsageCollector()
    collector.observe_message(
        {
            "role": "assistant",
            "usage": {"input": "many", "output": -3, "totalTokens": True},
        }
    )
    collector.observe_session_stats({"tokens": {"input": None}})

    assert collector.observed_records == 0
    assert collector.evidence().state is UsageState.UNMEASURED

    # A role spelled beyond any sane bound must not fail the session; the
    # reading it carries is still worth keeping.
    collector.observe_message({"role": "r" * 4_096, "usage": {"input": 7}})
    evidence = collector.evidence()
    assert evidence.state is UsageState.MEASURED
    assert evidence.requests[0].role == "unknown"
    assert evidence.requests[0].input_tokens == 7
