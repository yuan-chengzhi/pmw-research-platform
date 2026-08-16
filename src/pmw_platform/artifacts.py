"""Independent content-addressed storage for mathematical artifacts.

The PMW world stores immutable references and mathematical claims.  Artifact
bytes live here so a long-lived world does not depend on an old campaign
worktree.  Import copies bytes; it never symlinks or hardlinks back to the
source that may later be retired.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Iterable, NoReturn

from .world.records import canonical_json


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_REF = re.compile(r"^artifact/sha256/([0-9a-f]{64})$")
_RECEIPT_REF = re.compile(r"^artifact-receipt/sha256/([0-9a-f]{64})$")
MAXIMUM_RECEIPT_BYTES = 1_048_576


class ArtifactStoreError(ValueError):
    """The requested CAS operation was unsafe or failed validation."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _fail(code: str, detail: str = "") -> NoReturn:
    raise ArtifactStoreError(code, detail)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path, *, parent: Path | None = None) -> Path:
    try:
        if path.is_symlink():
            _fail("UNSAFE_STORE_PATH", str(path))
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        selected = path.resolve(strict=True)
    except ArtifactStoreError:
        raise
    except OSError as error:
        raise ArtifactStoreError("STORE_PATH_UNAVAILABLE", str(path)) from error
    if not selected.is_dir() or (parent is not None and selected.parent != parent):
        _fail("UNSAFE_STORE_PATH", str(path))
    return selected


def _digest_from_ref(reference: object) -> str:
    if type(reference) is not str:
        _fail("MALFORMED_ARTIFACT_REF")
    match = _ARTIFACT_REF.fullmatch(reference)
    if match is None:
        _fail("MALFORMED_ARTIFACT_REF", str(reference))
    return match.group(1)


def _hash_file(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ArtifactStoreError("ARTIFACT_UNAVAILABLE", str(path)) from error
    digest = hashlib.sha256()
    count = 0
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail("ARTIFACT_NOT_REGULAR", str(path))
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), count


def _strict_receipt(raw: bytes, *, filename_digest: str) -> dict[str, object]:
    if not raw or len(raw) > MAXIMUM_RECEIPT_BYTES:
        _fail("RECEIPT_SIZE_INVALID")

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        selected: dict[str, object] = {}
        for key, value in pairs:
            if key in selected:
                _fail("RECEIPT_JSON_INVALID", f"duplicate key: {key}")
            selected[key] = value
        return selected

    def reject_number(_value: str) -> NoReturn:
        _fail("RECEIPT_JSON_INVALID", "floating-point value")

    def reject_constant(value: str) -> NoReturn:
        _fail("RECEIPT_JSON_INVALID", value)

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_float=reject_number,
            parse_constant=reject_constant,
        )
    except ArtifactStoreError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ArtifactStoreError("RECEIPT_JSON_INVALID") from error
    if type(value) is not dict:
        _fail("RECEIPT_JSON_INVALID", "root")
    if raw != canonical_json(value) + b"\n":
        _fail("RECEIPT_NONCANONICAL")

    artifact_ref = value.get("artifact_ref")
    artifact_digest = _digest_from_ref(artifact_ref)
    receipt_ref = value.get("receipt_ref")
    match = _RECEIPT_REF.fullmatch(receipt_ref) if type(receipt_ref) is str else None
    byte_count = value.get("bytes")
    if (
        match is None
        or match.group(1) != filename_digest
        or value.get("sha256") != artifact_digest
        or type(byte_count) is not int
        or byte_count < 0
    ):
        _fail("RECEIPT_IDENTITY_INVALID", filename_digest)
    core = dict(value)
    del core["receipt_ref"]
    if hashlib.sha256(canonical_json(core)).hexdigest() != filename_digest:
        _fail("RECEIPT_IDENTITY_INVALID", "core digest")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactObject:
    artifact_ref: str
    sha256: str
    bytes: int
    path: Path


@dataclass(frozen=True, slots=True)
class ArtifactImport:
    source_label: str
    object_count: int
    object_bytes: int
    receipt_count: int
    receipt_bytes: int
    manifest_sha256: str
    manifest_path: Path


class ArtifactStore:
    """A no-follow, copy-owning SHA-256 object store."""

    def __init__(self, data_root: str | os.PathLike[str]) -> None:
        supplied = Path(data_root).expanduser()
        try:
            if supplied.is_symlink():
                _fail("UNSAFE_STORE_PATH", str(supplied))
            supplied.mkdir(mode=0o700, parents=True, exist_ok=True)
            root = supplied.resolve(strict=True)
        except ArtifactStoreError:
            raise
        except OSError as error:
            raise ArtifactStoreError(
                "STORE_PATH_UNAVAILABLE", str(supplied)
            ) from error
        self.data_root = root
        self.root = _ensure_directory(root / "objects", parent=root)
        self.objects = _ensure_directory(self.root / "sha256", parent=self.root)
        receipts = _ensure_directory(
            self.root / "artifact-receipts", parent=self.root
        )
        self.receipts = _ensure_directory(
            receipts / "sha256", parent=receipts
        )
        self.imports = _ensure_directory(self.root / "imports", parent=self.root)

    def object_path(self, artifact_ref: str) -> Path:
        return self.objects / _digest_from_ref(artifact_ref)

    def resolve(self, artifact_ref: str) -> ArtifactObject:
        digest = _digest_from_ref(artifact_ref)
        path = self.objects / digest
        actual, count = _hash_file(path)
        if actual != digest:
            _fail("ARTIFACT_HASH_MISMATCH", artifact_ref)
        return ArtifactObject(artifact_ref, digest, count, path)

    def exists(self, artifact_ref: str) -> bool:
        digest = _digest_from_ref(artifact_ref)
        path = self.objects / digest
        try:
            self.resolve(artifact_ref)
        except ArtifactStoreError:
            return False
        return True

    def copy_object(
        self,
        source: str | os.PathLike[str],
        *,
        expected_ref: str | None = None,
    ) -> ArtifactObject:
        """Copy and independently own one regular file by its exact bytes."""

        selected = Path(source)
        expected_digest = (
            None if expected_ref is None else _digest_from_ref(expected_ref)
        )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            source_fd = os.open(selected, flags)
        except OSError as error:
            raise ArtifactStoreError("ARTIFACT_UNAVAILABLE", str(selected)) from error
        temporary_fd: int | None = None
        temporary_path: str | None = None
        digest = hashlib.sha256()
        count = 0
        try:
            metadata = os.fstat(source_fd)
            if not stat.S_ISREG(metadata.st_mode):
                _fail("ARTIFACT_NOT_REGULAR", str(selected))
            temporary_fd, temporary_path = tempfile.mkstemp(
                prefix=".artifact.", dir=self.objects
            )
            os.fchmod(temporary_fd, 0o600)
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                count += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(temporary_fd, view)
                    view = view[written:]
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None
            actual = digest.hexdigest()
            if expected_digest is not None and actual != expected_digest:
                _fail("ARTIFACT_HASH_MISMATCH", str(selected))
            destination = self.objects / actual
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                prior, prior_count = _hash_file(destination)
                if prior != actual or prior_count != count:
                    _fail("ARTIFACT_COLLISION", actual)
            os.unlink(temporary_path)
            temporary_path = None
            _fsync_directory(self.objects)
            return ArtifactObject(
                artifact_ref=f"artifact/sha256/{actual}",
                sha256=actual,
                bytes=count,
                path=destination,
            )
        finally:
            os.close(source_fd)
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def import_legacy(
        self,
        source_root: str | os.PathLike[str],
        *,
        source_label: str,
    ) -> ArtifactImport:
        """Validate and copy one historical host artifact store byte-for-byte."""

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", source_label):
            _fail("SOURCE_LABEL_INVALID")
        supplied_source = Path(source_root)
        try:
            if supplied_source.is_symlink():
                _fail("UNSAFE_IMPORT_PATH")
            source = supplied_source.resolve(strict=True)
        except ArtifactStoreError:
            raise
        except OSError as error:
            raise ArtifactStoreError("IMPORT_PATH_UNAVAILABLE") from error
        source_objects = source / "objects" / "sha256"
        source_receipts = source / "receipts"
        if any(path.is_symlink() for path in (source, source_objects, source_receipts)):
            _fail("UNSAFE_IMPORT_PATH")
        if not source_objects.is_dir() or not source_receipts.is_dir():
            _fail("IMPORT_LAYOUT_INVALID")

        validated: list[
            tuple[Path, bytes, dict[str, object], str, Path, int]
        ] = []
        object_digests: set[str] = set()
        object_bytes = 0
        receipt_bytes = 0
        for receipt_path in sorted(source_receipts.iterdir(), key=lambda row: row.name):
            match = re.fullmatch(r"([0-9a-f]{64})\.json", receipt_path.name)
            if match is None or receipt_path.is_symlink() or not receipt_path.is_file():
                _fail("IMPORT_LAYOUT_INVALID", receipt_path.name)
            try:
                raw = receipt_path.read_bytes()
            except OSError as error:
                raise ArtifactStoreError(
                    "RECEIPT_UNAVAILABLE", receipt_path.name
                ) from error
            row = _strict_receipt(raw, filename_digest=match.group(1))
            artifact_digest = _digest_from_ref(row["artifact_ref"])
            if artifact_digest in object_digests:
                _fail("IMPORT_NOT_ONE_TO_ONE", artifact_digest)
            source_object = source_objects / artifact_digest
            actual_digest, actual_bytes = _hash_file(source_object)
            if actual_digest != artifact_digest:
                _fail("ARTIFACT_HASH_MISMATCH", artifact_digest)
            if actual_bytes != row["bytes"]:
                _fail("RECEIPT_SIZE_MISMATCH", artifact_digest)
            validated.append(
                (
                    receipt_path,
                    raw,
                    row,
                    artifact_digest,
                    source_object,
                    actual_bytes,
                )
            )
            object_digests.add(artifact_digest)
            object_bytes += actual_bytes
            receipt_bytes += len(raw)

        source_names = {path.name for path in source_objects.iterdir()}
        if source_names != object_digests or any(
            _DIGEST.fullmatch(name) is None for name in source_names
        ):
            _fail("IMPORT_NOT_ONE_TO_ONE")

        receipt_rows: list[dict[str, object]] = []
        for (
            receipt_path,
            raw,
            row,
            _artifact_digest,
            source_object,
            _actual_bytes,
        ) in validated:
            copied = self.copy_object(
                source_object, expected_ref=str(row["artifact_ref"])
            )
            self._copy_exact_bytes(raw, self.receipts / receipt_path.name)
            receipt_rows.append({
                "artifact_ref": copied.artifact_ref,
                "bytes": copied.bytes,
                "receipt_ref": row["receipt_ref"],
                "receipt_file_sha256": hashlib.sha256(raw).hexdigest(),
            })
        manifest: dict[str, object] = {
            "schema": "PMW_ARTIFACT_IMPORT_1",
            "source_label": source_label,
            "object_count": len(object_digests),
            "object_bytes": object_bytes,
            "receipt_count": len(receipt_rows),
            "receipt_bytes": receipt_bytes,
            "entries": receipt_rows,
        }
        manifest_bytes = canonical_json(manifest) + b"\n"
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        manifest_path = self.imports / f"{source_label}-{manifest_digest}.json"
        self._copy_exact_bytes(manifest_bytes, manifest_path)
        return ArtifactImport(
            source_label=source_label,
            object_count=len(object_digests),
            object_bytes=object_bytes,
            receipt_count=len(receipt_rows),
            receipt_bytes=receipt_bytes,
            manifest_sha256=manifest_digest,
            manifest_path=manifest_path,
        )

    def audit_refs(self, references: Iterable[str]) -> tuple[str, ...]:
        missing = sorted({ref for ref in references if not self.exists(ref)})
        return tuple(missing)

    def _copy_exact_bytes(self, raw: bytes, destination: Path) -> None:
        if destination.is_symlink():
            _fail("UNSAFE_STORE_PATH", str(destination))
        if destination.exists():
            if destination.read_bytes() != raw:
                _fail("OBJECT_COLLISION", destination.name)
            return
        descriptor: int | None = None
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{destination.name}.", dir=destination.parent
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.read_bytes() != raw:
                    _fail("OBJECT_COLLISION", destination.name)
            os.unlink(temporary)
            temporary = None
            _fsync_directory(destination.parent)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
