from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from pmw_platform.runtime.context import ContextWindowPolicy
from pmw_platform.runtime.contracts import runtime_host_policy_value
from pmw_platform.runtime.store import (
    RUNTIME_STATE_SCHEMA,
    RuntimeClaim,
    RuntimeStore,
    RuntimeStoreError,
)
from pmw_platform.runtime.usage import UsageEvidence
from pmw_platform.verifier_kit import (
    disabled_verifier_kit_launch_value,
    disabled_verifier_kit_session_evidence,
)
from pmw_platform.world.records import canonical_json


def _launch(session_ids: list[str]) -> dict[str, object]:
    backend = {
        "name": "local-test",
        "protocol": "TEST_1",
        "public_config": {"implementation": "tests"},
    }
    publication = {
        "schema": "PMW_RUNTIME_PUBLICATION_IDENTITY_1",
        "mode": "DISABLED",
        "protocol": "NO_PUBLICATION_1",
        "public_config": {},
    }
    readiness = {
        "schema": "PMW_RUNTIME_REQUIRED_READINESS_1",
        "checks": [],
    }
    verifier_kit = disabled_verifier_kit_launch_value()
    return {
        "schema": "PMW_RUNTIME_LAUNCH_1",
        "created_at": "2026-08-16T00:00:00Z",
        "cohort_id": "cohort-test",
        "world_id": "world-test",
        "world_ref": "refs/pmw/world-test",
        "base_snapshot_ref": f"snapshot/sha256/{'b' * 64}",
        "plan_sha256": "a" * 64,
        "briefing_sha256": "e" * 64,
        "safety_profile": "research-default",
        "safety_profile_sha256": "f" * 64,
        "core_lock_sha256": "c" * 64,
        "backend_sha256": hashlib.sha256(canonical_json(backend)).hexdigest(),
        "backend": backend,
        "publication_sha256": hashlib.sha256(
            canonical_json(publication)
        ).hexdigest(),
        "publication": publication,
        "concurrency": min(2, len(session_ids)),
        "session_ids": session_ids,
        "limits": {
            "startup_seconds": 60.0,
            "session_wall_seconds": 86400.0,
            "stop_grace_seconds": 10.0,
        },
        "context_window_policy": ContextWindowPolicy().bind(session_ids),
        "backend_context_window_control": "NOT_APPLICABLE",
        "required_readiness": readiness,
        "required_readiness_sha256": hashlib.sha256(
            canonical_json(readiness)
        ).hexdigest(),
        "verifier_kit": verifier_kit,
        "verifier_kit_sha256": hashlib.sha256(
            canonical_json(verifier_kit)
        ).hexdigest(),
        "host_policy": runtime_host_policy_value(),
    }


def _receipt(
    store: RuntimeStore, session_id: str, *, status: str = "SUCCEEDED"
) -> dict[str, object]:
    outcome = None
    if status in {"SUCCEEDED", "FAILED"}:
        outcome = {
            "success": status == "SUCCEEDED",
            "terminal_reason": "TEST_COMPLETE",
            "summary": "test outcome",
            "usage": {},
            "evidence": {},
            "contribution_count": 0,
            "contribution_sha256": [],
        }
    stopped = status != "UNKNOWN"
    return {
        "schema": "PMW_RUNTIME_SESSION_RECEIPT_1",
        "launch_sha256": store.launch_sha256(),
        "plan_sha256": "a" * 64,
        "backend_sha256": store.read_launch()["backend_sha256"],
        "cohort_id": "cohort-test",
        "session_id": session_id,
        "world_id": "world-test",
        "world_ref": "refs/pmw/world-test",
        "base_snapshot_ref": f"snapshot/sha256/{'b' * 64}",
        "status": status,
        "terminal_reason": "TEST_COMPLETE",
        "started_at": None,
        "finished_at": "2026-08-16T00:00:00Z",
        "stop_proof": {
            "stopped": stopped,
            "reason": "TEST_COMPLETE",
            "forced": False,
            "process_group_id": None,
            "detail": "",
        },
        "outcome": outcome,
        "publications": [],
        "error": None,
        "resource_guard": {
            "schema": "PMW_RUNTIME_RESOURCE_EVIDENCE_1",
            "checks": {"disk": 1, "workspace": 2, "cache": 2},
            "latest": {
                "disk": {
                    "total_bytes": 100,
                    "available_bytes": 80,
                    "required_free_bytes": 10,
                },
                "workspace": {
                    "total_bytes": 0,
                    "entries": 0,
                    "maximum_depth": 0,
                },
                "cache": {
                    "total_bytes": 0,
                    "entries": 0,
                    "maximum_depth": 0,
                },
            },
            "terminal_event": None,
            "warnings": [],
        },
        "usage": UsageEvidence.unmeasured(
            provenance="NO_BACKEND_OUTCOME",
            detail="test receipt",
        ).to_value(),
        "verifier_kit": disabled_verifier_kit_session_evidence(),
        "context_window": {
            "semantics": (
                "ACTIVE_MODEL_CONTEXT_WINDOW_TOKENS_NOT_CUMULATIVE_SESSION_USAGE"
            ),
            "configured_tokens": None,
            "backend_control": "NOT_APPLICABLE",
            "strict_pre_http_input_gate": False,
        },
    }


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_layout_canonical_documents_and_compact_status(tmp_path: Path) -> None:
    cohort = tmp_path / "cohort"
    cohort.mkdir(mode=0o700)
    store = RuntimeStore(cohort)
    launch = _launch(["s-1", "s-2"])

    with RuntimeClaim(cohort):
        launch_digest = store.create_launch(
            launch, session_ids=["s-1", "s-2"]
        )
        assert launch_digest == hashlib.sha256(
            canonical_json(launch) + b"\n"
        ).hexdigest()

        for session_id in ("s-1", "s-2"):
            paths = store.session_paths(session_id)
            for directory in (
                paths.root,
                paths.private,
                paths.input,
                paths.workspace,
                paths.cache,
                paths.evidence,
            ):
                assert directory.is_dir()
                assert _mode(directory) == 0o700
            assert _mode(paths.state) == 0o600
            assert store.read_state(session_id) == {
                "schema": RUNTIME_STATE_SCHEMA,
                "session_id": session_id,
                "launch_sha256": launch_digest,
                "state": "PLANNED",
            }

        invocation = store.write_input_file(
            "s-1", "invocation.json", b'{"hello":"world"}\n', mode=0o400
        )
        assert invocation.read_bytes() == b'{"hello":"world"}\n'
        assert _mode(invocation) == 0o400

        store.write_state(
            "s-1",
            {
                "schema": RUNTIME_STATE_SCHEMA,
                "session_id": "s-1",
                "launch_sha256": launch_digest,
                "state": "RUNNING",
                "attempt": 1,
            },
        )
        assert store.read_state("s-1")["state"] == "RUNNING"

        receipt_digests = {}
        for session_id, terminal in (("s-1", "SUCCEEDED"), ("s-2", "FAILED")):
            receipt = _receipt(store, session_id, status=terminal)
            digest = store.write_receipt(session_id, receipt)
            assert store.write_receipt(session_id, receipt) == digest
            receipt_digests[session_id] = digest

        settlement = {
            "schema": "PMW_RUNTIME_SETTLEMENT_1",
            "launch_sha256": launch_digest,
            "plan_sha256": "a" * 64,
            "cohort_id": "cohort-test",
            "finished_at": "2026-08-16T00:00:01Z",
            "outcome": "COMPLETED_WITH_FAILURES",
            "counts": {
                "CANCELLED": 0,
                "FAILED": 1,
                "SUCCEEDED": 1,
                "UNKNOWN": 0,
            },
            "receipts": [
                {
                    "session_id": session_id,
                    "status": "SUCCEEDED" if session_id == "s-1" else "FAILED",
                    "receipt_sha256": receipt_digests[session_id],
                }
                for session_id in ("s-1", "s-2")
            ],
        }
        settlement_digest = store.write_settlement(settlement)
        assert store.write_settlement(settlement) == settlement_digest

    status = store.read_status()
    assert status == {
        "launch_sha256": launch_digest,
        "sessions": [
            {
                "session_id": "s-1",
                "state": "RUNNING",
                "receipt_status": "SUCCEEDED",
                "receipt_sha256": receipt_digests["s-1"],
            },
            {
                "session_id": "s-2",
                "state": "PLANNED",
                "receipt_status": "FAILED",
                "receipt_sha256": receipt_digests["s-2"],
            },
        ],
        "settled": True,
        "settlement_sha256": settlement_digest,
    }
    assert (cohort / "runtime" / "launch.json").read_bytes() == (
        canonical_json(launch) + b"\n"
    )
    for path in cohort.glob("runtime/**/*.json"):
        assert _mode(path) in {0o400, 0o600}


def test_launch_never_reuses_an_existing_runtime(tmp_path: Path) -> None:
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    store = RuntimeStore(cohort)
    launch = _launch(["s-1"])
    store.create_launch(launch, session_ids=["s-1"])

    with pytest.raises(RuntimeStoreError) as caught:
        store.create_launch(launch, session_ids=["s-1"])
    assert caught.value.code == "RUNTIME_PATH_OCCUPIED"


@pytest.mark.parametrize("mutation", ["extra-field", "sensitive-backend-config"])
def test_launch_rejects_nonexact_or_invalid_public_identity(
    tmp_path: Path, mutation: str
) -> None:
    cohort = tmp_path / mutation
    cohort.mkdir()
    launch = _launch(["s-1"])
    if mutation == "extra-field":
        launch["unexpected"] = True
    else:
        backend = launch["backend"]
        assert type(backend) is dict
        backend["public_config"] = {"access_token": "must-not-be-public"}
        launch["backend_sha256"] = hashlib.sha256(
            canonical_json(backend)
        ).hexdigest()

    with pytest.raises(RuntimeStoreError) as caught:
        RuntimeStore(cohort).create_launch(launch, session_ids=["s-1"])

    assert caught.value.code == "MALFORMED_RUNTIME_LAUNCH"
    assert not (cohort / "runtime").exists()


def test_receipt_conflict_and_settlement_must_cover_exact_order(tmp_path: Path) -> None:
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    store = RuntimeStore(cohort)
    store.create_launch(_launch(["a", "b"]), session_ids=["a", "b"])
    digest_a = store.write_receipt("a", _receipt(store, "a"))
    digest_b = store.write_receipt("b", _receipt(store, "b"))

    conflicting = _receipt(store, "a", status="FAILED")
    with pytest.raises(RuntimeStoreError) as caught:
        store.write_receipt("a", conflicting)
    assert caught.value.code == "SESSION_RECEIPT_CONFLICT"

    for rows, expected_code in (
        (
            [
                {
                    "session_id": "a",
                    "status": "SUCCEEDED",
                    "receipt_sha256": digest_a,
                }
            ],
            "MALFORMED_SETTLEMENT",
        ),
        (
            [
                {
                    "session_id": "b",
                    "status": "SUCCEEDED",
                    "receipt_sha256": digest_b,
                },
                {
                    "session_id": "a",
                    "status": "SUCCEEDED",
                    "receipt_sha256": digest_a,
                },
            ],
            "MALFORMED_SETTLEMENT",
        ),
        (
            [
                {
                    "session_id": "a",
                    "status": "SUCCEEDED",
                    "receipt_sha256": "0" * 64,
                },
                {
                    "session_id": "b",
                    "status": "SUCCEEDED",
                    "receipt_sha256": digest_b,
                },
            ],
            "SETTLEMENT_RECEIPT_MISMATCH",
        ),
    ):
        with pytest.raises(RuntimeStoreError) as caught:
            store.write_settlement(
                {
                    "schema": "PMW_RUNTIME_SETTLEMENT_1",
                    "launch_sha256": store.launch_sha256(),
                    "plan_sha256": "a" * 64,
                    "cohort_id": "cohort-test",
                    "finished_at": "2026-08-16T00:00:01Z",
                    "outcome": "SUCCEEDED",
                    "counts": {
                        "CANCELLED": 0,
                        "FAILED": 0,
                        "SUCCEEDED": 2,
                        "UNKNOWN": 0,
                    },
                    "receipts": rows,
                }
            )
        assert caught.value.code == expected_code


def test_settlement_outcome_prioritizes_cancellation_over_failure(
    tmp_path: Path,
) -> None:
    cohort = tmp_path / "mixed"
    cohort.mkdir()
    store = RuntimeStore(cohort)
    store.create_launch(_launch(["failed", "cancelled"]), session_ids=["failed", "cancelled"])
    failed_digest = store.write_receipt(
        "failed", _receipt(store, "failed", status="FAILED")
    )
    cancelled_digest = store.write_receipt(
        "cancelled", _receipt(store, "cancelled", status="CANCELLED")
    )
    settlement = {
        "schema": "PMW_RUNTIME_SETTLEMENT_1",
        "launch_sha256": store.launch_sha256(),
        "plan_sha256": "a" * 64,
        "cohort_id": "cohort-test",
        "finished_at": "2026-08-16T00:00:01Z",
        "outcome": "COMPLETED_WITH_FAILURES",
        "counts": {
            "CANCELLED": 1,
            "FAILED": 1,
            "SUCCEEDED": 0,
            "UNKNOWN": 0,
        },
        "receipts": [
            {
                "session_id": "failed",
                "status": "FAILED",
                "receipt_sha256": failed_digest,
            },
            {
                "session_id": "cancelled",
                "status": "CANCELLED",
                "receipt_sha256": cancelled_digest,
            },
        ],
    }

    with pytest.raises(RuntimeStoreError) as caught:
        store.write_settlement(settlement)
    assert caught.value.code == "MALFORMED_SETTLEMENT"

    settlement["outcome"] = "CANCELLED"
    settlement["unexpected"] = True
    with pytest.raises(RuntimeStoreError) as caught:
        store.write_settlement(settlement)
    assert caught.value.code == "MALFORMED_SETTLEMENT"
    del settlement["unexpected"]

    rows = settlement["receipts"]
    assert type(rows) is list and type(rows[0]) is dict
    rows[0]["unexpected"] = True
    with pytest.raises(RuntimeStoreError) as caught:
        store.write_settlement(settlement)
    assert caught.value.code == "MALFORMED_SETTLEMENT"
    del rows[0]["unexpected"]

    assert store.write_settlement(settlement)


def test_rejects_symlinks_path_escape_and_nonterminal_receipts(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(RuntimeStoreError) as caught:
        RuntimeStore(alias)
    assert caught.value.code == "UNSAFE_RUNTIME_PATH"

    cohort = tmp_path / "cohort"
    cohort.mkdir()
    store = RuntimeStore(cohort)
    store.create_launch(_launch(["safe"]), session_ids=["safe"])
    with pytest.raises(RuntimeStoreError):
        store.session_paths("../escape")
    with pytest.raises(RuntimeStoreError):
        store.write_input_file("safe", "../escape", b"x")
    with pytest.raises(RuntimeStoreError) as caught:
        store.write_receipt("safe", _receipt(store, "safe", status="RUNNING"))
    assert caught.value.code == "MALFORMED_SESSION_RECEIPT"

    target = tmp_path / "outside-state"
    target.write_text("untouched")
    state = store.session_paths("safe").state
    state.unlink()
    state.symlink_to(target)
    with pytest.raises(RuntimeStoreError) as caught:
        store.write_state(
            "safe",
            {
                "schema": RUNTIME_STATE_SCHEMA,
                "session_id": "safe",
                "launch_sha256": store.launch_sha256(),
                "state": "RUNNING",
            },
        )
    assert caught.value.code == "UNSAFE_RUNTIME_PATH"
    assert target.read_text() == "untouched"


def test_receipt_schema_binds_world_identity_and_terminal_invariants(
    tmp_path: Path,
) -> None:
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    store = RuntimeStore(cohort)
    store.create_launch(_launch(["safe"]), session_ids=["safe"])

    invalid_receipts: list[dict[str, object]] = []
    for field, replacement in (
        ("world_id", "other-world"),
        ("world_ref", "refs/pmw/other-world"),
        ("base_snapshot_ref", f"snapshot/sha256/{'c' * 64}"),
    ):
        selected = _receipt(store, "safe")
        selected[field] = replacement
        invalid_receipts.append(selected)

    extra = _receipt(store, "safe")
    extra["unexpected"] = True
    invalid_receipts.append(extra)

    succeeded_without_outcome = _receipt(store, "safe")
    succeeded_without_outcome["outcome"] = None
    invalid_receipts.append(succeeded_without_outcome)

    succeeded_without_stop = _receipt(store, "safe")
    succeeded_without_stop["stop_proof"] = None
    invalid_receipts.append(succeeded_without_stop)

    succeeded_with_error = _receipt(store, "safe")
    succeeded_with_error["error"] = {"code": "INJECTED", "type": None}
    invalid_receipts.append(succeeded_with_error)

    cancelled_without_stop = _receipt(store, "safe", status="CANCELLED")
    cancelled_without_stop["stop_proof"] = None
    invalid_receipts.append(cancelled_without_stop)

    failed_with_unproven_stop = _receipt(store, "safe", status="FAILED")
    assert type(failed_with_unproven_stop["stop_proof"]) is dict
    failed_with_unproven_stop["stop_proof"]["stopped"] = False
    invalid_receipts.append(failed_with_unproven_stop)

    unknown_with_positive_stop = _receipt(store, "safe", status="UNKNOWN")
    assert type(unknown_with_positive_stop["stop_proof"]) is dict
    unknown_with_positive_stop["stop_proof"]["stopped"] = True
    invalid_receipts.append(unknown_with_positive_stop)

    malformed_outcome = _receipt(store, "safe")
    assert type(malformed_outcome["outcome"]) is dict
    malformed_outcome["outcome"]["contribution_count"] = 1
    invalid_receipts.append(malformed_outcome)

    disabled_success_with_contribution = _receipt(store, "safe")
    assert type(disabled_success_with_contribution["outcome"]) is dict
    disabled_success_with_contribution["outcome"]["contribution_count"] = 1
    disabled_success_with_contribution["outcome"]["contribution_sha256"] = [
        "d" * 64
    ]
    invalid_receipts.append(disabled_success_with_contribution)

    disabled_with_publication = _receipt(store, "safe", status="FAILED")
    disabled_with_publication["publications"] = [{"admission": "forged"}]
    invalid_receipts.append(disabled_with_publication)

    malformed_stop = _receipt(store, "safe")
    assert type(malformed_stop["stop_proof"]) is dict
    del malformed_stop["stop_proof"]["forced"]
    invalid_receipts.append(malformed_stop)

    malformed_resource_guard = _receipt(store, "safe")
    assert type(malformed_resource_guard["resource_guard"]) is dict
    del malformed_resource_guard["resource_guard"]["warnings"]
    invalid_receipts.append(malformed_resource_guard)

    for receipt in invalid_receipts:
        with pytest.raises(RuntimeStoreError) as caught:
            store.write_receipt("safe", receipt)
        assert caught.value.code == "MALFORMED_SESSION_RECEIPT"


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"])
def test_receipt_accepts_each_host_terminal_shape(
    tmp_path: Path, status: str
) -> None:
    cohort = tmp_path / status.lower()
    cohort.mkdir()
    store = RuntimeStore(cohort)
    store.create_launch(_launch(["safe"]), session_ids=["safe"])
    receipt = _receipt(store, "safe", status=status)

    digest = store.write_receipt("safe", receipt)

    assert digest == store.receipt_sha256("safe")


def test_runtime_claim_is_nonblocking_across_processes(tmp_path: Path) -> None:
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    source_root = Path(__file__).parents[1] / "src"
    script = """
import json
import sys
from pmw_platform.runtime.store import RuntimeClaim, RuntimeStoreError
try:
    with RuntimeClaim(sys.argv[1]):
        print(json.dumps({"result": "acquired"}))
except RuntimeStoreError as error:
    print(json.dumps({"result": "error", "code": error.code}))
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)

    with RuntimeClaim(cohort):
        blocked = subprocess.run(
            [sys.executable, "-c", script, str(cohort)],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    assert json.loads(blocked.stdout) == {
        "result": "error",
        "code": "RUNTIME_CLAIM_HELD",
    }

    acquired = subprocess.run(
        [sys.executable, "-c", script, str(cohort)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert json.loads(acquired.stdout) == {"result": "acquired"}
