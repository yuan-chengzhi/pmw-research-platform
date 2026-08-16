from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import MappingProxyType

import pytest

from pmw_platform.apparatus import persist_verification_receipt
from pmw_platform.artifacts import ArtifactStore
from pmw_platform.source_lock import CoreLock, LockedSource
from pmw_platform.source_materializer import MaterializedSource, SourceMaterializer
from pmw_platform.verifier import (
    AmfVerifierService,
    TargetVerifierBinding,
    VerifierServiceError,
    VerifierStatus,
)
from pmw_platform.world.records import canonical_json


VERIFIER_ID = "amf.fixture.exact.v1"
TARGET_ID = "fixture-target"
TARGET_SHA256 = "a" * 64


CHECKER = b"""#!/usr/bin/env python3
import argparse
import json
import sys

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
        path = raw_path.decode("utf-8")
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
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    files.sort(key=lambda value: str(value["path"]).encode("utf-8"))
    return hashlib.sha256(
        canonical_json({"files": files, "git_tree": tree})
    ).hexdigest()


def _amf_tree(
    tmp_path: Path,
    *,
    checker: bytes = CHECKER,
    timeout_seconds: int = 3,
    maximum_output_bytes: int = 4_096,
    corrupt_manifest_pin: bool = False,
    candidate_argument: str = "{candidate_path}",
) -> tuple[Path, str]:
    root = tmp_path / "amf"
    verifier = root / "verifiers" / VERIFIER_ID
    data = root / "data"
    verifier.mkdir(parents=True)
    data.mkdir()
    checker_path = verifier / "check.py"
    checker_path.write_bytes(checker)
    checker_path.chmod(0o755)
    checker_binding = {
        "path": f"verifiers/{VERIFIER_ID}/check.py",
        "bytes": len(checker),
        "sha256": hashlib.sha256(checker).hexdigest(),
    }
    manifest = {
        "schema": "AMF_VERIFIER_MANIFEST_1",
        "verifier_id": VERIFIER_ID,
        "binds_verification_mode": "synthetic_exact_check",
        "version": "v1",
        "command": [checker_binding["path"], "--candidate", candidate_argument],
        "working_directory": ".",
        "timeout_seconds": timeout_seconds,
        "maximum_output_bytes": maximum_output_bytes,
        "network": False,
        "source_artifacts": [checker_binding],
    }
    manifest_raw = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    manifest_path = verifier / "manifest.json"
    manifest_path.write_bytes(manifest_raw)
    manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
    registry = {
        "schema": "AMF_VERIFIER_REGISTRY_1",
        "verifiers": [
            {
                "verifier_id": VERIFIER_ID,
                "protocol": "AMF_VERIFIER_PROTOCOL_1",
                "manifest": {
                    "path": f"verifiers/{VERIFIER_ID}/manifest.json",
                    "bytes": len(manifest_raw),
                    "sha256": (
                        "0" * 64 if corrupt_manifest_pin else manifest_digest
                    ),
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


def _service(
    tmp_path: Path,
    repository: Path,
    commit: str,
) -> tuple[AmfVerifierService, ArtifactStore, MaterializedSource]:
    locked = LockedSource(
        "agent-math-frontier",
        "https://example.invalid/agent-math-frontier.git",
        commit,
        "problem-and-verifier-authority",
        _locked_tree_digest(repository, commit),
    )
    core_lock = CoreLock("b" * 64, MappingProxyType({locked.name: locked}))
    data_root = tmp_path / "runtime-data"
    materializer = SourceMaterializer(data_root, core_lock=core_lock)
    materialized = materializer.ensure(
        "agent-math-frontier", local_repository=repository
    )
    registry = json.loads(
        (materialized.tree_path / "data" / "verifiers.json").read_text()
    )
    registry_sha256 = hashlib.sha256(canonical_json(registry)).hexdigest()
    manifest_row = registry["verifiers"][0]["manifest"]
    manifest = json.loads(
        (materialized.tree_path / manifest_row["path"]).read_text()
    )
    store = ArtifactStore(data_root)
    return (
        AmfVerifierService(
            source_materializer=materializer,
            artifact_store=store,
            target_bindings=[
                TargetVerifierBinding(
                    target_id=TARGET_ID,
                    target_sha256=TARGET_SHA256,
                    verification_mode=manifest["binds_verification_mode"],
                    verifier_id=VERIFIER_ID,
                    registry_sha256=registry_sha256,
                    manifest_path=manifest_row["path"],
                    manifest_sha256=manifest_row["sha256"],
                )
            ],
            python_executable=sys.executable,
            cleanup_grace_seconds=1,
        ),
        store,
        materialized,
    )


def _candidate(workspace: Path, value: object) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / "candidate.json"
    path.write_text(json.dumps(value, sort_keys=True))
    return path


def test_host_reexecution_is_authoritative_and_captures_candidate(
    tmp_path: Path,
) -> None:
    repository, commit = _amf_tree(tmp_path)
    service, store, materialized = _service(tmp_path, repository, commit)
    retired_repository = tmp_path / "retired-development-worktree"
    repository.rename(retired_repository)
    assert not (materialized.tree_path / ".git").exists()
    workspace = tmp_path / "workspace"

    accepted_path = _candidate(
        workspace, {"agent_claim": "REJECTED", "mathematical_value": 7}
    )
    accepted_raw = accepted_path.read_bytes()
    accepted = service.verify(
        session_id="session-a",
        session_workspace=workspace,
        target_id=TARGET_ID,
        candidate_relative_path="candidate.json",
    )
    assert accepted.status is VerifierStatus.PASS
    assert store.resolve(accepted.candidate.artifact_ref).path.read_bytes() == accepted_raw
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    receipt_path = persist_verification_receipt(evidence, accepted)
    assert receipt_path.read_bytes() == canonical_json(accepted.as_dict()) + b"\n"
    assert persist_verification_receipt(evidence, accepted) == receipt_path

    _candidate(workspace, {"agent_claim": "PASS", "mathematical_value": 6})
    rejected = service.verify(
        session_id="session-a",
        session_workspace=workspace,
        target_id=TARGET_ID,
        candidate_relative_path="candidate.json",
    )
    assert rejected.status is VerifierStatus.REJECTED
    assert rejected.diagnostic_code == "CANDIDATE_REJECTED"
    value = rejected.as_dict()
    assert value["authority"] == "HOST_REEXECUTED_PINNED_AMF_VERIFIER"
    assert value["target"] == {
        "target_id": TARGET_ID,
        "target_sha256": TARGET_SHA256,
        "verification_mode": "synthetic_exact_check",
    }
    assert value["source_tree"]["commit"] == commit
    assert value["source_tree"]["git_tree"] == materialized.git_tree
    assert value["source_tree"]["materializer"] == {
        "tree_sha256": materialized.tree_sha256,
        "manifest_sha256": materialized.manifest_sha256,
    }
    assert value["verifier"]["manifest_sha256"]
    assert value["interpreter"]["sha256"] == service.identity.interpreter.sha256
    core = dict(value)
    receipt_ref = core.pop("receipt_ref")
    assert receipt_ref == (
        "verifier-receipt/sha256/" + hashlib.sha256(canonical_json(core)).hexdigest()
    )


def test_candidate_must_be_workspace_relative_and_nofollow(tmp_path: Path) -> None:
    root, commit = _amf_tree(tmp_path)
    service, _store, _materialized = _service(tmp_path, root, commit)
    workspace = tmp_path / "workspace"
    real = workspace / "real"
    _candidate(real, {"mathematical_value": 7})
    (workspace / "linked").symlink_to(real, target_is_directory=True)

    with pytest.raises(VerifierServiceError) as parent_link:
        service.verify(
            session_id="session-a",
            session_workspace=workspace,
            target_id=TARGET_ID,
            candidate_relative_path="linked/candidate.json",
        )
    assert parent_link.value.code == "UNSAFE_CANDIDATE_PATH"

    direct_link = workspace / "candidate-link.json"
    direct_link.symlink_to(real / "candidate.json")
    with pytest.raises(VerifierServiceError) as final_link:
        service.verify(
            session_id="session-a",
            session_workspace=workspace,
            target_id=TARGET_ID,
            candidate_relative_path="candidate-link.json",
        )
    assert final_link.value.code == "UNSAFE_CANDIDATE_PATH"

    with pytest.raises(VerifierServiceError, match="UNSAFE_CANDIDATE_PATH"):
        service.verify(
            session_id="session-a",
            session_workspace=workspace,
            target_id=TARGET_ID,
            candidate_relative_path="../outside.json",
        )


def test_source_drift_fails_closed_with_typed_apparatus_receipt(
    tmp_path: Path,
) -> None:
    root, commit = _amf_tree(tmp_path)
    service, _store, materialized = _service(tmp_path, root, commit)
    workspace = tmp_path / "workspace"
    _candidate(workspace, {"mathematical_value": 7})
    checker = materialized.tree_path / "verifiers" / VERIFIER_ID / "check.py"
    materialized.root.chmod(0o755)
    materialized.tree_path.chmod(0o755)
    checker.chmod(0o644)
    checker.write_bytes(checker.read_bytes() + b"\n# drift\n")
    checker.chmod(0o555)
    materialized.tree_path.chmod(0o555)
    materialized.root.chmod(0o555)

    receipt = service.verify(
        session_id="session-a",
        session_workspace=workspace,
        target_id=TARGET_ID,
        candidate_relative_path="candidate.json",
    )
    assert receipt.status is VerifierStatus.APPARATUS_ERROR
    assert receipt.diagnostic_code == "SOURCE_CACHE_CONFLICT"
    assert receipt.as_dict()["execution"]["process_state"] == "PRECHECK_FAILED"
    assert receipt.as_dict()["verifier_output"] is None


def test_registry_manifest_and_source_pins_are_checked_at_load(
    tmp_path: Path,
) -> None:
    root, commit = _amf_tree(tmp_path, corrupt_manifest_pin=True)
    with pytest.raises(VerifierServiceError) as failure:
        _service(tmp_path, root, commit)
    assert failure.value.code == "AMF_VERIFIER_MANIFEST_PIN_MISMATCH"


def test_candidate_placeholder_must_be_one_exact_argument(tmp_path: Path) -> None:
    root, commit = _amf_tree(
        tmp_path,
        candidate_argument="--input={candidate_path}",
    )

    with pytest.raises(VerifierServiceError) as failure:
        _service(tmp_path, root, commit)

    assert failure.value.code == "AMF_VERIFIER_MANIFEST_INVALID"


def test_timeout_cleans_the_cooperative_process_group(tmp_path: Path) -> None:
    checker = b"""#!/usr/bin/env python3
import subprocess
import sys
import time
subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])
time.sleep(30)
"""
    root, commit = _amf_tree(tmp_path, checker=checker, timeout_seconds=1)
    service, _store, _materialized = _service(tmp_path, root, commit)
    workspace = tmp_path / "workspace"
    _candidate(workspace, {"mathematical_value": 7})

    receipt = service.verify(
        session_id="session-a",
        session_workspace=workspace,
        target_id=TARGET_ID,
        candidate_relative_path="candidate.json",
    )
    execution = receipt.as_dict()["execution"]
    assert receipt.status is VerifierStatus.APPARATUS_ERROR
    assert receipt.diagnostic_code == "TIMEOUT"
    assert execution["cleanup_attempted"] is True
    assert execution["cleanup_complete"] is True


def test_output_and_top_level_network_are_failed_closed(tmp_path: Path) -> None:
    noisy = b"""#!/usr/bin/env python3
import os
os.write(1, b"x" * 65536)
"""
    root, commit = _amf_tree(
        tmp_path / "output", checker=noisy, maximum_output_bytes=64
    )
    service, store, _materialized = _service(tmp_path / "output", root, commit)
    workspace = tmp_path / "output" / "workspace"
    _candidate(workspace, {"mathematical_value": 7})
    receipt = service.verify(
        session_id="session-a",
        session_workspace=workspace,
        target_id=TARGET_ID,
        candidate_relative_path="candidate.json",
    )
    execution = receipt.as_dict()["execution"]
    retained = execution["stdout"]["retained_bytes"] + execution["stderr"]["retained_bytes"]
    assert receipt.status is VerifierStatus.APPARATUS_ERROR
    assert receipt.diagnostic_code == "OUTPUT_LIMIT_EXCEEDED"
    assert retained <= 64
    store.resolve(execution["stdout"]["artifact_ref"])

    network = b"""#!/usr/bin/env python3
import socket
socket.socket()
"""
    net_root, net_commit = _amf_tree(tmp_path / "network", checker=network)
    net_service, _net_store, _net_materialized = _service(
        tmp_path / "network", net_root, net_commit
    )
    net_workspace = tmp_path / "network" / "workspace"
    _candidate(net_workspace, {"mathematical_value": 7})
    denied = net_service.verify(
        session_id="session-a",
        session_workspace=net_workspace,
        target_id=TARGET_ID,
        candidate_relative_path="candidate.json",
    )
    assert denied.status is VerifierStatus.APPARATUS_ERROR
    assert denied.diagnostic_code == "VERIFIER_STDERR_NONEMPTY"
    denied_execution = denied.as_dict()["execution"]
    assert denied_execution["manifest_network"] is False
    assert denied_execution["top_level_python_socket_audit"] is True
    assert denied_execution["os_network_isolation"] is False

    large_result = b"""#!/usr/bin/env python3
import json
print(json.dumps({
    "accepted": True,
    "facts": "x" * 40000,
    "reason_code": "LARGE_ACCEPTED_RESULT",
    "schema": "AMF_VERIFIER_RESULT_1",
    "verifier_id": "amf.fixture.exact.v1",
}, sort_keys=True, separators=(",", ":")))
"""
    projection_root, projection_commit = _amf_tree(
        tmp_path / "projection",
        checker=large_result,
        maximum_output_bytes=65_536,
    )
    projection_service, projection_store, _projection_materialized = _service(
        tmp_path / "projection", projection_root, projection_commit
    )
    projection_workspace = tmp_path / "projection" / "workspace"
    _candidate(projection_workspace, {"mathematical_value": 7})
    projected = projection_service.verify(
        session_id="session-a",
        session_workspace=projection_workspace,
        target_id=TARGET_ID,
        candidate_relative_path="candidate.json",
    )
    projected_value = projected.as_dict()
    assert projected.status is VerifierStatus.PASS
    assert "facts" not in projected_value["verifier_output"]
    assert projected_value["verifier_output_binding"]["projection"] == (
        "CORE_FIELDS_ONLY_FULL_VALUE_IN_STDOUT_CAS"
    )
    projection_store.resolve(
        projected_value["verifier_output_binding"]["full_stdout_artifact_ref"]
    )
