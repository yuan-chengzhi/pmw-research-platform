from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import MappingProxyType, SimpleNamespace

import pytest

from pmw_platform.artifacts import ArtifactStore
from pmw_platform.runtime.command import CommandBackend, CommandBackendConfig
from pmw_platform.runtime.auth import PreparedCohort
from pmw_platform.runtime.context import ContextWindowControl
from pmw_platform.runtime.contracts import (
    BackendIdentity,
    BackendStartError,
    StopProof,
)
from pmw_platform.runtime.orchestrator import (
    RuntimeLimits,
    run_prepared_cohort,
)
from pmw_platform.runtime.safety import load_named_profile
from pmw_platform.runtime import store as store_module
from pmw_platform.runtime.store import RuntimeStore
from pmw_platform.sessions import CohortPlan
from pmw_platform.source_lock import CoreLock, LockedSource, load_core_lock
from pmw_platform.source_materializer import SourceMaterializer
from pmw_platform.verifier import (
    AmfVerifierService,
    TargetVerifierBinding,
    VerifierStatus,
)
from pmw_platform.verifier_kit import (
    IN_SESSION_VERIFICATION_AUTHORITY,
    KIT_DIRECTORY_NAME,
    KIT_ENTRYPOINT,
    KIT_EVIDENCE_DIRECTORY_NAME,
    SETTLEMENT_VERIFICATION_AUTHORITY,
    VERDICT_STATUSES,
    VERIFIER_KIT_INVOCATION_SCHEMA,
    VERIFIER_KIT_LAUNCH_SCHEMA,
    VERIFIER_KIT_SESSION_EVIDENCE_SCHEMA,
    VERIFIER_KIT_VERDICT_SCHEMA,
    VerifierKit,
    build_verifier_kit,
    read_session_verifier_kit_evidence,
)
from pmw_platform.world.records import canonical_json


VERIFIER_ID = "amf.fixture.exact.v1"
TARGET_ID = "fixture-target"
SECOND_TARGET_ID = "fixture-target-two"
TARGET_SHA256 = "a" * 64

CHECKER = b"""#!/usr/bin/env python3
import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument("--candidate", required=True)
args = parser.parse_args()
with open(args.candidate, "rb") as stream:
    value = json.load(stream)
accepted = value.get("mathematical_value") == 7
print(json.dumps({
    "accepted": accepted,
    "reason_code": "EXACT_SEVEN" if accepted else "NOT_SEVEN",
    "schema": "AMF_VERIFIER_RESULT_1",
    "verifier_id": "amf.fixture.exact.v1",
}, sort_keys=True, separators=(",", ":")))
raise SystemExit(0 if accepted else 1)
"""


def _run_git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "LC_ALL": "C",
        },
    )
    return completed.stdout.decode().strip()


def _locked_tree_digest(root: Path, commit: str) -> str:
    tree = _run_git(root, "rev-parse", f"{commit}^{{tree}}")
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "-z", "--full-tree", commit],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    files: list[dict[str, object]] = []
    for record in listed.split(b"\0"):
        if not record:
            continue
        identity, raw_path = record.split(b"\t", 1)
        raw_mode, _kind, raw_blob = identity.split(b" ", 2)
        content = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", raw_blob.decode("ascii")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
        files.append(
            {
                "bytes": len(content),
                "git_blob": raw_blob.decode("ascii"),
                "git_mode": raw_mode.decode("ascii"),
                "path": raw_path.decode("utf-8"),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    files.sort(key=lambda value: str(value["path"]).encode("utf-8"))
    return hashlib.sha256(
        canonical_json({"files": files, "git_tree": tree})
    ).hexdigest()


def _amf_repository(root: Path) -> tuple[Path, str]:
    verifier = root / "verifiers" / VERIFIER_ID
    data = root / "data"
    verifier.mkdir(parents=True)
    data.mkdir()
    checker_path = verifier / "check.py"
    checker_path.write_bytes(CHECKER)
    checker_path.chmod(0o755)
    checker_binding = {
        "path": f"verifiers/{VERIFIER_ID}/check.py",
        "bytes": len(CHECKER),
        "sha256": hashlib.sha256(CHECKER).hexdigest(),
    }
    manifest = {
        "schema": "AMF_VERIFIER_MANIFEST_1",
        "verifier_id": VERIFIER_ID,
        "binds_verification_mode": "synthetic_exact_check",
        "version": "v1",
        "command": [checker_binding["path"], "--candidate", "{candidate_path}"],
        "working_directory": ".",
        "timeout_seconds": 30,
        "maximum_output_bytes": 65_536,
        "network": False,
        "source_artifacts": [checker_binding],
    }
    manifest_raw = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    (verifier / "manifest.json").write_bytes(manifest_raw)
    registry = {
        "schema": "AMF_VERIFIER_REGISTRY_1",
        "verifiers": [
            {
                "verifier_id": VERIFIER_ID,
                "protocol": "AMF_VERIFIER_PROTOCOL_1",
                "manifest": {
                    "path": f"verifiers/{VERIFIER_ID}/manifest.json",
                    "bytes": len(manifest_raw),
                    "sha256": hashlib.sha256(manifest_raw).hexdigest(),
                },
            }
        ],
    }
    (data / "verifiers.json").write_text(
        json.dumps(registry, indent=2, sort_keys=True) + "\n"
    )
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.invalid")
    _run_git(root, "config", "user.name", "Fixture")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture verifier")
    return root, _run_git(root, "rev-parse", "HEAD")


def _materializer(data_root: Path, repository: Path, commit: str) -> SourceMaterializer:
    locked = LockedSource(
        "agent-math-frontier",
        "https://example.invalid/agent-math-frontier.git",
        commit,
        "problem-and-verifier-authority",
        _locked_tree_digest(repository, commit),
    )
    core_lock = CoreLock("b" * 64, MappingProxyType({locked.name: locked}))
    materializer = SourceMaterializer(data_root, core_lock=core_lock)
    materializer.ensure("agent-math-frontier", local_repository=repository)
    return materializer


def _bindings(materializer: SourceMaterializer, *target_ids: str) -> list[
    TargetVerifierBinding
]:
    tree = materializer.audit("agent-math-frontier").tree_path
    registry = json.loads((tree / "data" / "verifiers.json").read_text())
    registry_sha256 = hashlib.sha256(canonical_json(registry)).hexdigest()
    row = registry["verifiers"][0]["manifest"]
    return [
        TargetVerifierBinding(
            target_id=target_id,
            target_sha256=TARGET_SHA256,
            verification_mode="synthetic_exact_check",
            verifier_id=VERIFIER_ID,
            registry_sha256=registry_sha256,
            manifest_path=row["path"],
            manifest_sha256=row["sha256"],
        )
        for target_id in target_ids
    ]


def _kit(tmp_path: Path, *target_ids: str) -> tuple[VerifierKit, SourceMaterializer, Path]:
    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    repository, commit = _amf_repository(tmp_path / "amf")
    materializer = _materializer(data_root, repository, commit)
    kit = build_verifier_kit(
        source_materializer=materializer,
        target_bindings=_bindings(materializer, *(target_ids or (TARGET_ID,))),
        python_executable=sys.executable,
    )
    return kit, materializer, data_root


def _prepared(
    data_root: Path,
    *,
    cohort_id: str,
    count: int,
    concurrency: int | None = None,
) -> PreparedCohort:
    profile = load_named_profile("research-default")
    core = load_core_lock()
    briefing = b'{"schema":"TEST_BRIEFING_1"}\n'
    plan = CohortPlan.generate(
        cohort_id=cohort_id,
        world_id="math-frontier",
        world_ref="refs/pmw/math-frontier",
        base_snapshot_ref="snapshot/sha256/" + "a" * 64,
        safety_profile=profile.name,
        safety_profile_sha256=profile.sha256,
        core_lock_sha256=core.sha256,
        briefing_sha256=hashlib.sha256(briefing).hexdigest(),
        count=count,
        concurrency=count if concurrency is None else concurrency,
    )
    cohort_root = data_root / "runs" / cohort_id
    cohort_root.mkdir(parents=True)
    return PreparedCohort(
        data_root=data_root,
        cohort_root=cohort_root,
        plan_path=cohort_root / "plan.json",
        briefing_path=cohort_root / "briefing.json",
        briefing_bytes=briefing,
        plan=plan,
        profile=profile,
        core_lock=core,
        registration=SimpleNamespace(
            name="math-frontier",
            repo=str(data_root / "world.git"),
            world_ref="refs/pmw/math-frontier",
        ),
        world=SimpleNamespace(),
        artifact_store=SimpleNamespace(exists=lambda _ref: True),
    )


WORKER = f"""#!/bin/sh
# Zero-model command worker: write one candidate, then use the in-session kit.
set -u
printf '%s' '{{"mathematical_value": 7}}' > candidate.json
if "./{KIT_ENTRYPOINT}" candidate.json > kit-stdout.json 2> kit-stderr.txt
then
  kit_status=0
else
  kit_status=$?
fi
cat > "$PMW_RESULT_PATH" <<JSON
{{"schema":"PMW_RUNTIME_BACKEND_OUTCOME_1","success":true,\
"terminal_reason":"MODEL_FREE_KIT_ACCEPTANCE_COMPLETED",\
"summary":"invoked the in-session verifier kit once",\
"usage":{{"model_calls":0}},\
"evidence":{{"kit_exit_status":$kit_status}},"contributions":[]}}
JSON
"""


def _command_backend(tmp_path: Path, script: str) -> CommandBackend:
    worker = tmp_path / "kit-worker.sh"
    worker.write_text(script)
    worker.chmod(0o755)
    return CommandBackend(
        CommandBackendConfig.from_value(
            {
                "schema": "PMW_COMMAND_BACKEND_CONFIG_1",
                "name": "kit-worker",
                "argv": [str(worker)],
                "argv_is_public": True,
                "result_path": "result.json",
                "capture": {
                    "maximum_retained_bytes": 1_048_576,
                    "maximum_observed_bytes": 1_073_741_824,
                    "tail_bytes": 4_096,
                },
            }
        )
    )


def test_command_session_invokes_the_kit_and_settlement_stays_authoritative(
    tmp_path: Path,
) -> None:
    kit, materializer, data_root = _kit(tmp_path)
    prepared = _prepared(data_root, cohort_id="cohort-kit-e2e", count=1)
    backend = _command_backend(tmp_path, WORKER)

    result = asyncio.run(
        run_prepared_cohort(
            prepared,
            backend,
            limits=RuntimeLimits(
                startup_seconds=60.0,
                session_wall_seconds=300.0,
                stop_grace_seconds=5.0,
            ),
            verifier_kit=kit,
        )
    )

    assert result.outcome == "SUCCEEDED"
    store = RuntimeStore(prepared.cohort_root)
    launch = store.read_launch()
    kit_identity = launch["verifier_kit"]
    assert isinstance(kit_identity, dict)
    assert kit_identity["schema"] == VERIFIER_KIT_LAUNCH_SCHEMA
    assert kit_identity["mode"] == "MATERIALIZED"
    assert kit_identity["kit_sha256"] == kit.sha256
    assert kit_identity["credential_material"] is False
    assert kit_identity["target_count"] == 1
    assert kit_identity["target_ids_sha256"] == kit.target_ids_sha256
    assert len(canonical_json(kit_identity)) <= 8_192
    assert launch["verifier_kit_sha256"] == hashlib.sha256(
        canonical_json(kit_identity)
    ).hexdigest()

    session_id = prepared.plan.sessions[0].session_id
    receipt = store.read_receipt(session_id)
    assert receipt is not None
    evidence = receipt["verifier_kit"]
    assert isinstance(evidence, dict)
    assert evidence["schema"] == VERIFIER_KIT_SESSION_EVIDENCE_SCHEMA
    assert evidence["authority"] == IN_SESSION_VERIFICATION_AUTHORITY
    assert evidence["ledger"] == "OBSERVED"
    assert evidence["invocation_count"] == 1
    assert evidence["verdict_counts"] == {
        "PASS": 1,
        "REJECTED": 0,
        "APPARATUS_ERROR": 0,
    }
    assert evidence["rejected_entries"] == 0
    assert evidence["kit_content_sha256"] == kit.content_sha256

    paths = store.session_paths(session_id)
    verdict = json.loads(
        (
            paths.workspace / KIT_EVIDENCE_DIRECTORY_NAME / "verdicts" / "000001.json"
        ).read_text()
    )
    assert verdict["schema"] == VERIFIER_KIT_VERDICT_SCHEMA
    assert verdict["authority"] == IN_SESSION_VERIFICATION_AUTHORITY
    assert verdict["settlement_authority"] == SETTLEMENT_VERIFICATION_AUTHORITY
    assert verdict["status"] == "PASS"
    assert verdict["candidate"]["workspace_relative_path"] == "candidate.json"
    invocation = json.loads(
        (
            paths.workspace / KIT_EVIDENCE_DIRECTORY_NAME / "receipts" / "000001.json"
        ).read_text()
    )
    assert invocation["schema"] == VERIFIER_KIT_INVOCATION_SCHEMA
    assert invocation["status"] == "PASS"
    assert invocation["execution"]["credential_inheritance"] is False
    assert invocation["verdict_path"] == "verdicts/000001.json"

    # The prompt surface every backend reads announces the capability.
    invocation_document = json.loads((paths.input / "invocation.json").read_text())
    announcement = invocation_document["verifier_kit"]
    assert announcement["available"] is True
    assert announcement["authority"] == IN_SESSION_VERIFICATION_AUTHORITY
    assert announcement["invocation"]["command"].startswith(f"./{KIT_ENTRYPOINT}")
    assert announcement["target_ids"] == [TARGET_ID]
    assert announcement["target_ids_complete"] is True

    # Settlement-side authority is unchanged and reaches the same verdict on
    # the same bytes without consulting any in-session evidence.
    service = AmfVerifierService(
        source_materializer=materializer,
        artifact_store=ArtifactStore(data_root),
        target_bindings=_bindings(materializer, TARGET_ID),
        python_executable=sys.executable,
    )
    host = service.verify(
        session_id=session_id,
        session_workspace=paths.workspace,
        target_id=TARGET_ID,
        candidate_relative_path="candidate.json",
    )
    assert host.status is VerifierStatus.PASS
    assert host.as_dict()["authority"] == "HOST_REEXECUTED_PINNED_AMF_VERIFIER"


def test_absent_kit_is_announced_and_counted_as_disabled(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    prepared = _prepared(data_root, cohort_id="cohort-kit-absent", count=1)
    worker = """#!/bin/sh
set -eu
test ! -e ".pmw-verifier-kit"
cat > "$PMW_RESULT_PATH" <<'JSON'
{"schema":"PMW_RUNTIME_BACKEND_OUTCOME_1","success":true,\
"terminal_reason":"NO_KIT","summary":"no kit","usage":{},"evidence":{},\
"contributions":[]}
JSON
"""
    result = asyncio.run(
        run_prepared_cohort(
            prepared,
            _command_backend(tmp_path, worker),
            limits=RuntimeLimits(
                startup_seconds=60.0,
                session_wall_seconds=300.0,
                stop_grace_seconds=5.0,
            ),
        )
    )

    assert result.outcome == "SUCCEEDED"
    store = RuntimeStore(prepared.cohort_root)
    assert store.read_launch()["verifier_kit"] == {
        "schema": VERIFIER_KIT_LAUNCH_SCHEMA,
        "mode": "DISABLED",
        "authority": IN_SESSION_VERIFICATION_AUTHORITY,
        "settlement_authority": SETTLEMENT_VERIFICATION_AUTHORITY,
        "reason": "NO_IN_SESSION_VERIFIER_KIT_MATERIALIZED",
    }
    session_id = prepared.plan.sessions[0].session_id
    receipt = store.read_receipt(session_id)
    assert receipt is not None
    assert receipt["verifier_kit"]["mode"] == "DISABLED"  # type: ignore[index]
    assert receipt["verifier_kit"]["invocation_count"] == 0  # type: ignore[index]
    assert receipt["verifier_kit"]["ledger"] == "NOT_MATERIALIZED"  # type: ignore[index]
    paths = store.session_paths(session_id)
    announcement = json.loads((paths.input / "invocation.json").read_text())[
        "verifier_kit"
    ]
    assert announcement["available"] is False


def test_kit_is_read_only_path_independent_and_carries_no_host_data_root(
    tmp_path: Path,
) -> None:
    kit, _materializer, data_root = _kit(tmp_path)
    first = tmp_path / "workspace-one"
    second = tmp_path / "workspace-two"
    first.mkdir()
    second.mkdir()
    kit.materialize(first)
    kit.materialize(second)

    for item in kit.files:
        left = first / KIT_DIRECTORY_NAME / item.path
        right = second / KIT_DIRECTORY_NAME / item.path
        assert left.read_bytes() == item.content
        assert left.read_bytes() == right.read_bytes()
        assert stat.S_IMODE(left.lstat().st_mode) == item.mode
        assert not left.is_symlink()
    assert stat.S_IMODE((first / KIT_DIRECTORY_NAME).lstat().st_mode) == 0o555
    assert (first / KIT_EVIDENCE_DIRECTORY_NAME).is_dir()

    blob = b"".join(item.content for item in kit.files)
    assert str(data_root).encode("utf-8") not in blob
    assert b"source-cache" not in blob
    assert str(Path(sys.executable).resolve(strict=True)).encode("utf-8") in blob

    manifest = json.loads(
        (first / KIT_DIRECTORY_NAME / "manifest.json").read_text()
    )
    assert manifest["credential_material"] is False
    assert manifest["content_sha256"] == kit.content_sha256
    assert len(manifest["files"]) == kit.file_count - 1


def test_direct_invocation_covers_selection_rejection_and_path_escape(
    tmp_path: Path,
) -> None:
    kit, _materializer, _data_root = _kit(tmp_path, TARGET_ID, SECOND_TARGET_ID)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kit.materialize(workspace)
    wrapper = workspace / KIT_ENTRYPOINT

    def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(wrapper), *arguments],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )

    listed = invoke("--list-targets")
    assert listed.returncode == 0
    assert sorted(
        row["target_id"] for row in json.loads(listed.stdout)["targets"]
    ) == sorted([TARGET_ID, SECOND_TARGET_ID])

    (workspace / "candidate.json").write_text('{"mathematical_value": 7}')
    ambiguous = invoke("candidate.json")
    assert ambiguous.returncode == 64
    assert json.loads(ambiguous.stderr)["code"] == "TARGET_SELECTION_REQUIRED"

    accepted = invoke("--target", TARGET_ID, "candidate.json")
    assert accepted.returncode == 0
    assert json.loads(accepted.stdout)["status"] == "PASS"

    (workspace / "weak.json").write_text('{"mathematical_value": 6}')
    rejected = invoke("--target", SECOND_TARGET_ID, "weak.json")
    assert rejected.returncode == 1
    rejected_value = json.loads(rejected.stdout)
    assert rejected_value["status"] == "REJECTED"
    assert rejected_value["diagnostic_code"] == "CANDIDATE_REJECTED"
    assert rejected_value["target"]["target_id"] == SECOND_TARGET_ID

    outside = tmp_path / "outside.json"
    outside.write_text('{"mathematical_value": 7}')
    escaped = invoke("--target", TARGET_ID, str(outside))
    assert escaped.returncode == 2
    escaped_value = json.loads(escaped.stdout)
    assert escaped_value["status"] == "APPARATUS_ERROR"
    assert escaped_value["diagnostic_code"] == "UNSAFE_CANDIDATE_PATH"

    evidence = read_session_verifier_kit_evidence(kit, workspace)
    assert evidence["ledger"] == "OBSERVED"
    assert evidence["verdict_counts"] == {
        "PASS": 1,
        "REJECTED": 1,
        "APPARATUS_ERROR": 1,
    }
    assert evidence["invocation_count"] == 3
    ordinals = sorted(
        path.name
        for path in (workspace / KIT_EVIDENCE_DIRECTORY_NAME / "receipts").iterdir()
    )
    assert ordinals == ["000001.json", "000002.json", "000003.json"]


def test_ledger_counting_is_observational_not_authoritative(tmp_path: Path) -> None:
    kit, _materializer, _data_root = _kit(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kit.materialize(workspace)

    absent = read_session_verifier_kit_evidence(kit, workspace)
    assert absent["ledger"] == "ABSENT"
    assert absent["invocation_count"] == 0

    receipts = workspace / KIT_EVIDENCE_DIRECTORY_NAME / "receipts"
    receipts.mkdir(parents=True)
    genuine = {
        "schema": VERIFIER_KIT_INVOCATION_SCHEMA,
        "authority": IN_SESSION_VERIFICATION_AUTHORITY,
        "kit_content_sha256": kit.content_sha256,
        "status": "PASS",
    }
    (receipts / "000001.json").write_bytes(canonical_json(genuine) + b"\n")
    forged = dict(genuine, kit_content_sha256="0" * 64)
    (receipts / "000002.json").write_bytes(canonical_json(forged) + b"\n")
    (receipts / "000003.json").write_bytes(b"not json")
    promoted = dict(genuine, authority=SETTLEMENT_VERIFICATION_AUTHORITY)
    (receipts / "000004.json").write_bytes(canonical_json(promoted) + b"\n")

    observed = read_session_verifier_kit_evidence(kit, workspace)
    assert observed["ledger"] == "OBSERVED"
    assert observed["invocation_count"] == 1
    assert observed["rejected_entries"] == 3
    assert observed["counting_authority"] == (
        "HOST_OBSERVED_SESSION_LOCAL_ADVISORY_LEDGER_NOT_TAMPER_PROOF"
    )


class _UnprovenStartBackend:
    """A backend whose first start fails without proving cleanup."""

    def __init__(self) -> None:
        self._identity = BackendIdentity(
            name="unproven-start",
            protocol="FIXTURE_RUNTIME_1",
            public_config={"implementation": "tests"},
        )

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    @property
    def context_window_control(self) -> ContextWindowControl:
        return ContextWindowControl.NOT_APPLICABLE

    def verify_runtime(self) -> None:
        return None

    async def start(self, request: object) -> object:
        raise BackendStartError(
            "FIXTURE_START_FAILED",
            stop_proof=StopProof(stopped=False, reason="STOP_UNPROVEN"),
        )


def test_never_started_session_settles_with_an_absent_kit_ledger(
    tmp_path: Path,
) -> None:
    kit, _materializer, data_root = _kit(tmp_path)
    prepared = _prepared(
        data_root, cohort_id="cohort-kit-not-started", count=2, concurrency=1
    )

    result = asyncio.run(
        run_prepared_cohort(
            prepared,
            _UnprovenStartBackend(),
            limits=RuntimeLimits(
                startup_seconds=10.0,
                session_wall_seconds=60.0,
                stop_grace_seconds=1.0,
            ),
            verifier_kit=kit,
        )
    )

    # The point is that a kit-enabled cohort still settles every session: an
    # absent advisory ledger is a valid observation, not a settlement failure.
    assert result.outcome == "UNSAFE"
    store = RuntimeStore(prepared.cohort_root)
    ledgers = []
    for spec in prepared.plan.sessions:
        receipt = store.read_receipt(spec.session_id)
        assert receipt is not None
        evidence = receipt["verifier_kit"]
        assert isinstance(evidence, dict)
        assert evidence["mode"] == "MATERIALIZED"
        assert evidence["invocation_count"] == 0
        ledgers.append(evidence["ledger"])
    assert ledgers == ["ABSENT", "ABSENT"]


def test_kit_rejects_a_second_materialization_into_one_workspace(
    tmp_path: Path,
) -> None:
    kit, _materializer, _data_root = _kit(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kit.materialize(workspace)
    with pytest.raises(Exception) as failure:
        kit.materialize(workspace)
    assert getattr(failure.value, "code", "") == "VERIFIER_KIT_PATH_OCCUPIED"


def test_store_and_kit_share_one_in_session_vocabulary() -> None:
    assert store_module._VERIFIER_KIT_LAUNCH_SCHEMA == VERIFIER_KIT_LAUNCH_SCHEMA
    assert (
        store_module._VERIFIER_KIT_EVIDENCE_SCHEMA
        == VERIFIER_KIT_SESSION_EVIDENCE_SCHEMA
    )
    assert (
        store_module._IN_SESSION_VERIFIER_AUTHORITY
        == IN_SESSION_VERIFICATION_AUTHORITY
    )
    assert (
        store_module._SETTLEMENT_VERIFIER_AUTHORITY
        == SETTLEMENT_VERIFICATION_AUTHORITY
    )
    assert store_module._VERIFIER_VERDICT_STATUSES == frozenset(VERDICT_STATUSES)
