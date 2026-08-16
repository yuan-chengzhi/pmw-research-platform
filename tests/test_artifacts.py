from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from pmw_platform.artifacts import ArtifactStore, ArtifactStoreError
from pmw_platform.world.records import canonical_json


def _legacy_store(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "legacy"
    objects = root / "objects" / "sha256"
    receipts = root / "receipts"
    objects.mkdir(parents=True)
    receipts.mkdir()
    payload = b"exact mathematical evidence\n"
    artifact_digest = hashlib.sha256(payload).hexdigest()
    artifact_ref = f"artifact/sha256/{artifact_digest}"
    object_path = objects / artifact_digest
    object_path.write_bytes(payload)
    # A second link is ordinary filesystem state, not a platform threat.
    os.link(object_path, tmp_path / "second-link")

    core = {
        "artifact_ref": artifact_ref,
        "bytes": len(payload),
        "capture_authority": "HOST_NOFOLLOW_EXACT_BYTE_COPY",
        "claim_kind": "CODE",
        "life_id": "fixture",
        "mathematical_authority": "NONE_UNTIL_SEPARATE_VERIFICATION",
        "schema": "PMW_HOST_ARTIFACT_RECEIPT_2",
        "sha256": artifact_digest,
        "source_relative_path": "evidence.txt",
        "statement": "Fixture evidence.",
        "submission_kind": "AUXILIARY",
    }
    receipt_digest = hashlib.sha256(canonical_json(core)).hexdigest()
    receipt = dict(core)
    receipt["receipt_ref"] = f"artifact-receipt/sha256/{receipt_digest}"
    (receipts / f"{receipt_digest}.json").write_bytes(
        canonical_json(receipt) + b"\n"
    )
    return root, artifact_ref, receipt_digest


def test_legacy_import_copies_and_closes_references(tmp_path: Path) -> None:
    source, artifact_ref, receipt_digest = _legacy_store(tmp_path)
    data = tmp_path / "data"
    imported = ArtifactStore(data).import_legacy(
        source, source_label="fixture-wave"
    )

    store = ArtifactStore(data)
    resolved = store.resolve(artifact_ref)
    assert imported.object_count == 1
    assert imported.receipt_count == 1
    assert resolved.path.read_bytes() == b"exact mathematical evidence\n"
    assert resolved.path.stat().st_nlink == 1
    assert (
        data
        / "objects"
        / "artifact-receipts"
        / "sha256"
        / f"{receipt_digest}.json"
    ).is_file()
    manifest = json.loads(imported.manifest_path.read_text())
    assert manifest["entries"][0]["artifact_ref"] == artifact_ref
    assert store.audit_refs([artifact_ref]) == ()
    metadata = resolved.path.stat()
    resolved.path.write_bytes(b"x" * resolved.bytes)
    os.utime(
        resolved.path,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
    )
    assert not store.exists(artifact_ref)


def test_legacy_import_rejects_two_receipts_for_one_object(tmp_path: Path) -> None:
    source, artifact_ref, _receipt_digest = _legacy_store(tmp_path)
    original = json.loads(next((source / "receipts").iterdir()).read_text())
    core = dict(original)
    del core["receipt_ref"]
    core["statement"] = "A conflicting second receipt for the same object."
    duplicate_digest = hashlib.sha256(canonical_json(core)).hexdigest()
    duplicate = dict(core)
    duplicate["receipt_ref"] = (
        f"artifact-receipt/sha256/{duplicate_digest}"
    )
    (source / "receipts" / f"{duplicate_digest}.json").write_bytes(
        canonical_json(duplicate) + b"\n"
    )

    with pytest.raises(ArtifactStoreError, match="IMPORT_NOT_ONE_TO_ONE"):
        ArtifactStore(tmp_path / "data").import_legacy(
            source, source_label="duplicate"
        )
    assert artifact_ref.startswith("artifact/sha256/")
