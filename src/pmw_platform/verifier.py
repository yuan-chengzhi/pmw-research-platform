"""Host-authoritative execution of content-pinned AMF verifiers.

The agent-facing boundary is deliberately one-way: a session contributes a
workspace-relative candidate path, while the host captures those exact bytes
and independently runs a verifier selected by a host-owned target binding.
Agent prose (including a claimed ``PASS``) is never an input to the verdict.

This module is intentionally not a general-purpose sandbox.  The verifier
source is trusted only after ``SourceMaterializer`` re-audits its locked
repository, commit, Git tree, complete materialization and verifier pins.  It
receives no inherited credentials, top-level Python socket operations are
denied by an audit hook, and cooperative process-group cleanup bounds normal
verifier descendants.  Receipts explicitly say that no OS network isolation
is claimed; a stronger adapter can wrap this service without changing its
authority boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Mapping, NoReturn, Sequence

from .artifacts import ArtifactObject, ArtifactStore, ArtifactStoreError
from .source_materializer import (
    MaterializedSource,
    SourceMaterializer,
    SourceMaterializerError,
)
from .world.records import canonical_json


VERIFIER_RECEIPT_SCHEMA = "PMW_AMF_VERIFIER_RECEIPT_1"
VERIFIER_REGISTRY_SCHEMA = "AMF_VERIFIER_REGISTRY_1"
VERIFIER_MANIFEST_SCHEMA = "AMF_VERIFIER_MANIFEST_1"
VERIFIER_RESULT_SCHEMA = "AMF_VERIFIER_RESULT_1"
VERIFIER_PROTOCOL = "AMF_VERIFIER_PROTOCOL_1"
VERIFIER_AUTHORITY = "HOST_REEXECUTED_PINNED_AMF_VERIFIER"
SOURCE_TREE_PROTOCOL = "AUDITED_SOURCE_MATERIALIZATION_1"
NETWORK_POLICY = (
    "PINNED_TOP_LEVEL_PYTHON_AUDIT_SOCKET_DENY_NO_CREDENTIAL_ENV_1"
)

MAXIMUM_REGISTRY_BYTES = 1_048_576
MAXIMUM_MANIFEST_BYTES = 1_048_576
MAXIMUM_SOURCE_ARTIFACT_BYTES = 1_073_741_824
MAXIMUM_CANDIDATE_BYTES = 1_073_741_824
MAXIMUM_VERIFIER_OUTPUT_BYTES = 4_194_304
MAXIMUM_INLINE_VERIFIER_RESULT_BYTES = 32_768
MAXIMUM_INTERPRETER_BYTES = 1_073_741_824
MAXIMUM_TIMEOUT_SECONDS = 86_400

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERIFIER_ID = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,110}\.v[1-9][0-9]{0,15}$"
)

_OFFLINE_BOOTSTRAP = """\
import os, runpy, sys
def _pmw_audit(event, args):
    if event.startswith("socket."):
        raise PermissionError("PMW_OFFLINE_VERIFIER_SOCKET_DENIED")
sys.addaudithook(_pmw_audit)
_pmw_entry = sys.argv[1]
sys.path.insert(0, os.path.dirname(_pmw_entry))
sys.argv = sys.argv[1:]
runpy.run_path(_pmw_entry, run_name="__main__")
"""
_BOOTSTRAP_SHA256 = hashlib.sha256(_OFFLINE_BOOTSTRAP.encode("utf-8")).hexdigest()


class VerifierServiceError(ValueError):
    """A stable request, source-pin, or local-apparatus failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _fail(code: str, detail: str = "") -> NoReturn:
    raise VerifierServiceError(code, detail)


class VerifierStatus(str, Enum):
    """The complete authoritative outcome space for one invocation."""

    PASS = "PASS"
    REJECTED = "REJECTED"
    APPARATUS_ERROR = "APPARATUS_ERROR"


@dataclass(frozen=True, slots=True)
class TargetVerifierBinding:
    """Host-owned binding between one frozen target and one AMF verifier."""

    target_id: str
    target_sha256: str
    verification_mode: str
    verifier_id: str
    registry_sha256: str
    manifest_path: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        if type(self.target_id) is not str or _ID.fullmatch(self.target_id) is None:
            raise ValueError("target_id is malformed")
        if (
            type(self.target_sha256) is not str
            or _SHA256.fullmatch(self.target_sha256) is None
        ):
            raise ValueError("target_sha256 is malformed")
        if (
            type(self.verification_mode) is not str
            or not self.verification_mode
            or len(self.verification_mode.encode("utf-8")) > 512
        ):
            raise ValueError("verification_mode is malformed")
        if (
            type(self.verifier_id) is not str
            or _VERIFIER_ID.fullmatch(self.verifier_id) is None
        ):
            raise ValueError("verifier_id is malformed")
        if (
            type(self.registry_sha256) is not str
            or _SHA256.fullmatch(self.registry_sha256) is None
        ):
            raise ValueError("registry_sha256 is malformed")
        _relative(self.manifest_path, code="MALFORMED_TARGET_BINDING")
        if (
            type(self.manifest_sha256) is not str
            or _SHA256.fullmatch(self.manifest_sha256) is None
        ):
            raise ValueError("manifest_sha256 is malformed")


@dataclass(frozen=True, slots=True)
class InterpreterIdentity:
    path: str
    sha256: str
    bytes: int
    implementation: str
    version: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "implementation": self.implementation,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class CandidateCapture:
    workspace_relative_path: str
    artifact_ref: str
    sha256: str
    bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "workspace_relative_path": self.workspace_relative_path,
            "artifact_ref": self.artifact_ref,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "capture_authority": "HOST_NOFOLLOW_STABLE_COPY_TO_CAS",
        }


@dataclass(frozen=True, slots=True)
class StreamCapture:
    artifact_ref: str
    sha256: str
    retained_bytes: int
    observed_bytes: int
    truncated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_ref": self.artifact_ref,
            "sha256": self.sha256,
            "retained_bytes": self.retained_bytes,
            "observed_bytes": self.observed_bytes,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class VerificationReceipt:
    """A typed, content-identified host receipt.

    ``as_dict`` returns a detached JSON value so callers cannot mutate the
    receipt held by this object.
    """

    status: VerifierStatus
    receipt_ref: str
    session_id: str
    target_id: str
    candidate: CandidateCapture
    verifier_id: str
    diagnostic_code: str
    _document: dict[str, object] = field(repr=False)

    def as_dict(self) -> dict[str, object]:
        return json.loads(canonical_json(self._document).decode("utf-8"))


@dataclass(frozen=True, slots=True)
class VerifierServiceIdentity:
    source_name: str
    repository: str
    commit: str
    git_tree: str
    materializer_tree_sha256: str
    materializer_manifest_sha256: str
    registry_sha256: str
    interpreter: InterpreterIdentity
    verifier_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "source_name": self.source_name,
            "repository": self.repository,
            "commit": self.commit,
            "git_tree": self.git_tree,
            "materializer_tree_sha256": self.materializer_tree_sha256,
            "materializer_manifest_sha256": self.materializer_manifest_sha256,
            "registry_sha256": self.registry_sha256,
            "interpreter": self.interpreter.as_dict(),
            "verifier_ids": list(self.verifier_ids),
        }


@dataclass(frozen=True, slots=True)
class VerifierPortfolioIdentity:
    """Read-only identity of a briefing-bound verifier portfolio."""

    source_name: str
    repository: str
    commit: str
    git_tree: str
    materializer_tree_sha256: str
    materializer_manifest_sha256: str
    registry_sha256: str
    catalog_verifier_count: int
    target_count: int
    target_bindings_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "source_name": self.source_name,
            "repository": self.repository,
            "commit": self.commit,
            "git_tree": self.git_tree,
            "materializer_tree_sha256": self.materializer_tree_sha256,
            "materializer_manifest_sha256": self.materializer_manifest_sha256,
            "registry_sha256": self.registry_sha256,
            "catalog_verifier_count": self.catalog_verifier_count,
            "target_count": self.target_count,
            "target_bindings_sha256": self.target_bindings_sha256,
        }


@dataclass(frozen=True, slots=True)
class _SourceIdentity:
    name: str
    repository: str
    commit: str
    git_tree: str
    tree_sha256: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _VerifierDefinition:
    verifier_id: str
    verification_mode: str
    protocol: str
    registry_manifest_path: str
    manifest_sha256: str
    manifest_bytes: int
    command: tuple[str, ...]
    working_directory: str
    timeout_seconds: int
    maximum_output_bytes: int
    source_artifacts: tuple[tuple[str, bytes, str], ...]
    source_closure_sha256: str


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    identity: _SourceIdentity
    registry_sha256: str
    definitions: Mapping[str, _VerifierDefinition]


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    state: str
    exit_code: int | None
    signal_name: str | None
    elapsed_ms: int
    stdout: bytes
    stderr: bytes
    stdout_observed_bytes: int
    stderr_observed_bytes: int
    cleanup_attempted: bool
    cleanup_complete: bool
    cleanup_failure: str | None


def _strict_object(raw: bytes, *, code: str) -> dict[str, object]:
    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        selected: dict[str, object] = {}
        for key, value in pairs:
            if key in selected:
                raise ValueError("duplicate key")
            selected[key] = value
        return selected

    def reject_number(_value: str) -> NoReturn:
        raise ValueError("floating-point JSON is unsupported")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise VerifierServiceError(code) from error
    if type(value) is not dict:
        _fail(code, "root must be an object")
    try:
        canonical_json(value)
    except Exception as error:
        raise VerifierServiceError(code) from error
    return value


def _relative(value: object, *, code: str, allow_dot: bool = False) -> str:
    if type(value) is not str or not value or "\x00" in value or "\\" in value:
        _fail(code)
    pure = PurePosixPath(value)
    normalized = pure.as_posix()
    if allow_dot and value == ".":
        return value
    if (
        pure.is_absolute()
        or normalized != value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        _fail(code)
    return value


def _open_root(root: Path, *, code: str) -> int:
    if not root.is_absolute():
        _fail(code, "root must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(root, flags)
    except OSError as error:
        raise VerifierServiceError(code, "root cannot be opened") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        _fail(code, "root is not a directory")
    return descriptor


def _open_relative(
    root: Path,
    relative: str,
    *,
    code: str,
    directory: bool = False,
) -> int:
    selected = _relative(relative, code=code, allow_dot=directory)
    current = _open_root(root, code=code)
    if selected == ".":
        return current
    parts = PurePosixPath(selected).parts
    try:
        for part in parts[:-1]:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            next_descriptor = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_descriptor
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        result = os.open(parts[-1], flags, dir_fd=current)
    except OSError as error:
        raise VerifierServiceError(code, selected) from error
    finally:
        os.close(current)
    return result


def _stable_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_relative_regular(
    root: Path,
    relative: str,
    *,
    maximum_bytes: int,
    code: str,
) -> bytes:
    descriptor = _open_relative(root, relative, code=code)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= maximum_bytes:
            _fail(code, relative)
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) > maximum_bytes
            or len(raw) != before.st_size
            or _stable_metadata(before) != _stable_metadata(after)
        ):
            _fail(code, f"unstable file: {relative}")
        return raw
    finally:
        os.close(descriptor)


def _hash_absolute_regular(path: Path, *, maximum_bytes: int, code: str) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise VerifierServiceError(code) from error
    digest = hashlib.sha256()
    count = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum_bytes:
            _fail(code)
        while True:
            chunk = os.read(descriptor, 1_048_576)
            if not chunk:
                break
            count += len(chunk)
            if count > maximum_bytes:
                _fail(code)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if count != before.st_size or _stable_metadata(before) != _stable_metadata(after):
            _fail(code, "file changed while hashing")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), count


def _require_binding(value: object, *, code: str) -> tuple[str, int, str]:
    if type(value) is not dict or set(value) != {"path", "bytes", "sha256"}:
        _fail(code)
    path = _relative(value["path"], code=code)
    count = value["bytes"]
    digest = value["sha256"]
    if (
        type(count) is not int
        or count < 0
        or type(digest) is not str
        or _SHA256.fullmatch(digest) is None
    ):
        _fail(code)
    return path, count, digest


class AmfVerifierService:
    """Re-execute frozen AMF verifiers as a host authority."""

    def __init__(
        self,
        *,
        source_materializer: SourceMaterializer,
        artifact_store: ArtifactStore,
        target_bindings: Sequence[TargetVerifierBinding],
        python_executable: str | os.PathLike[str],
        source_name: str = "agent-math-frontier",
        maximum_candidate_bytes: int = MAXIMUM_CANDIDATE_BYTES,
        cleanup_grace_seconds: float = 2.0,
    ) -> None:
        if not isinstance(source_materializer, SourceMaterializer):
            raise TypeError("source_materializer must be a SourceMaterializer")
        if type(source_name) is not str or not source_name:
            raise ValueError("source_name is malformed")
        if not isinstance(artifact_store, ArtifactStore):
            raise TypeError("artifact_store must be an ArtifactStore")
        bindings = tuple(target_bindings)
        if not bindings or any(not isinstance(item, TargetVerifierBinding) for item in bindings):
            raise ValueError("target_bindings must be non-empty and typed")
        by_target = {item.target_id: item for item in bindings}
        if len(by_target) != len(bindings):
            raise ValueError("target_bindings contain duplicate targets")
        if (
            type(maximum_candidate_bytes) is not int
            or not 1 <= maximum_candidate_bytes <= MAXIMUM_CANDIDATE_BYTES
        ):
            raise ValueError("maximum_candidate_bytes is invalid")
        if (
            type(cleanup_grace_seconds) not in {int, float}
            or not 0.1 <= float(cleanup_grace_seconds) <= 30.0
        ):
            raise ValueError("cleanup_grace_seconds is invalid")

        selected_python = Path(python_executable).expanduser().resolve(strict=True)
        self.source_materializer = source_materializer
        self.source_name = source_name
        self.artifact_store = artifact_store
        self.bindings = MappingProxyType(by_target)
        self.python_executable = selected_python
        self.maximum_candidate_bytes = maximum_candidate_bytes
        self.cleanup_grace_seconds = float(cleanup_grace_seconds)

        self._interpreter = self._load_interpreter_identity()
        self._source = self._load_source_snapshot()
        self._validate_bindings(self._source, bindings)

    @staticmethod
    def _validate_bindings(
        source: _SourceSnapshot,
        bindings: Sequence[TargetVerifierBinding],
    ) -> None:
        unknown = sorted(
            item.verifier_id
            for item in bindings
            if item.verifier_id not in source.definitions
        )
        if unknown:
            raise VerifierServiceError("UNKNOWN_PINNED_VERIFIER", unknown[0])
        for binding in bindings:
            definition = source.definitions[binding.verifier_id]
            if binding.registry_sha256 != source.registry_sha256:
                raise VerifierServiceError(
                    "AMF_VERIFIER_REGISTRY_PIN_MISMATCH", binding.target_id
                )
            if (
                binding.manifest_path != definition.registry_manifest_path
                or binding.manifest_sha256 != definition.manifest_sha256
            ):
                raise VerifierServiceError(
                    "AMF_VERIFIER_MANIFEST_PIN_MISMATCH", binding.target_id
                )
            if binding.verification_mode != definition.verification_mode:
                raise VerifierServiceError(
                    "AMF_VERIFICATION_MODE_MISMATCH", binding.target_id
                )

    @classmethod
    def audit_portfolio(
        cls,
        *,
        source_materializer: SourceMaterializer,
        target_bindings: Sequence[TargetVerifierBinding],
        source_name: str = "agent-math-frontier",
    ) -> VerifierPortfolioIdentity:
        """Audit source, catalog and target pins without spawning a verifier.

        This is the production-preflight path.  It performs only bounded local
        reads through ``SourceMaterializer`` and does not construct an artifact
        store, probe an interpreter, or execute verifier code.
        """

        if not isinstance(source_materializer, SourceMaterializer):
            raise TypeError("source_materializer must be a SourceMaterializer")
        bindings = tuple(target_bindings)
        if not bindings or any(
            not isinstance(item, TargetVerifierBinding) for item in bindings
        ):
            raise ValueError("target_bindings must be non-empty and typed")
        if len({item.target_id for item in bindings}) != len(bindings):
            raise ValueError("target_bindings contain duplicate targets")
        inspector = object.__new__(cls)
        inspector.source_materializer = source_materializer
        inspector.source_name = source_name
        source = inspector._load_source_snapshot()
        cls._validate_bindings(source, bindings)
        binding_value = [
            {
                "target_id": item.target_id,
                "target_sha256": item.target_sha256,
                "verification_mode": item.verification_mode,
                "verifier_id": item.verifier_id,
                "registry_sha256": item.registry_sha256,
                "manifest_path": item.manifest_path,
                "manifest_sha256": item.manifest_sha256,
            }
            for item in sorted(bindings, key=lambda selected: selected.target_id)
        ]
        return VerifierPortfolioIdentity(
            source_name=source.identity.name,
            repository=source.identity.repository,
            commit=source.identity.commit,
            git_tree=source.identity.git_tree,
            materializer_tree_sha256=source.identity.tree_sha256,
            materializer_manifest_sha256=source.identity.manifest_sha256,
            registry_sha256=source.registry_sha256,
            catalog_verifier_count=len(source.definitions),
            target_count=len(bindings),
            target_bindings_sha256=hashlib.sha256(
                canonical_json(binding_value)
            ).hexdigest(),
        )

    @property
    def identity(self) -> VerifierServiceIdentity:
        return VerifierServiceIdentity(
            source_name=self._source.identity.name,
            repository=self._source.identity.repository,
            commit=self._source.identity.commit,
            git_tree=self._source.identity.git_tree,
            materializer_tree_sha256=self._source.identity.tree_sha256,
            materializer_manifest_sha256=self._source.identity.manifest_sha256,
            registry_sha256=self._source.registry_sha256,
            interpreter=self._interpreter,
            verifier_ids=tuple(sorted(self._source.definitions)),
        )

    def _audit_materialized_source(self) -> MaterializedSource:
        try:
            return self.source_materializer.audit(self.source_name)
        except SourceMaterializerError as error:
            raise VerifierServiceError(error.code, error.detail) from error

    def _load_source_snapshot(self) -> _SourceSnapshot:
        materialized = self._audit_materialized_source()
        source_root = materialized.tree_path
        identity = _SourceIdentity(
            name=materialized.name,
            repository=materialized.repository,
            commit=materialized.commit,
            git_tree=materialized.git_tree,
            tree_sha256=materialized.tree_sha256,
            manifest_sha256=materialized.manifest_sha256,
        )
        registry_raw = _read_relative_regular(
            source_root,
            "data/verifiers.json",
            maximum_bytes=MAXIMUM_REGISTRY_BYTES,
            code="AMF_VERIFIER_REGISTRY_UNAVAILABLE",
        )
        registry = _strict_object(registry_raw, code="AMF_VERIFIER_REGISTRY_INVALID")
        # AMF contracts bind the canonical registry value, while each registry
        # row deliberately binds the exact raw manifest bytes.
        registry_sha256 = hashlib.sha256(canonical_json(registry)).hexdigest()
        if set(registry) != {"schema", "verifiers"} or registry.get("schema") != VERIFIER_REGISTRY_SCHEMA:
            _fail("AMF_VERIFIER_REGISTRY_INVALID")
        entries = registry.get("verifiers")
        if type(entries) is not list or not entries or len(entries) > 1_024:
            _fail("AMF_VERIFIER_REGISTRY_INVALID")
        definitions: dict[str, _VerifierDefinition] = {}
        for entry in entries:
            if type(entry) is not dict or set(entry) != {"verifier_id", "protocol", "manifest"}:
                _fail("AMF_VERIFIER_REGISTRY_INVALID")
            verifier_id = entry.get("verifier_id")
            if type(verifier_id) is not str or _VERIFIER_ID.fullmatch(verifier_id) is None:
                _fail("AMF_VERIFIER_REGISTRY_INVALID")
            if entry.get("protocol") != VERIFIER_PROTOCOL or verifier_id in definitions:
                _fail("AMF_VERIFIER_REGISTRY_INVALID")
            manifest_path, manifest_bytes, manifest_sha256 = _require_binding(
                entry.get("manifest"), code="AMF_VERIFIER_REGISTRY_INVALID"
            )
            raw = _read_relative_regular(
                source_root,
                manifest_path,
                maximum_bytes=MAXIMUM_MANIFEST_BYTES,
                code="AMF_VERIFIER_MANIFEST_UNAVAILABLE",
            )
            if len(raw) != manifest_bytes or hashlib.sha256(raw).hexdigest() != manifest_sha256:
                _fail("AMF_VERIFIER_MANIFEST_PIN_MISMATCH", verifier_id)
            manifest = _strict_object(raw, code="AMF_VERIFIER_MANIFEST_INVALID")
            definitions[verifier_id] = self._validate_manifest(
                source_root=source_root,
                verifier_id=verifier_id,
                protocol=VERIFIER_PROTOCOL,
                manifest_path=manifest_path,
                manifest_sha256=manifest_sha256,
                manifest_bytes=manifest_bytes,
                manifest=manifest,
            )
        return _SourceSnapshot(
            identity=identity,
            registry_sha256=registry_sha256,
            definitions=MappingProxyType(definitions),
        )

    def _validate_manifest(
        self,
        *,
        source_root: Path,
        verifier_id: str,
        protocol: str,
        manifest_path: str,
        manifest_sha256: str,
        manifest_bytes: int,
        manifest: dict[str, object],
    ) -> _VerifierDefinition:
        expected = {
            "schema",
            "verifier_id",
            "binds_verification_mode",
            "version",
            "command",
            "working_directory",
            "timeout_seconds",
            "maximum_output_bytes",
            "network",
            "source_artifacts",
        }
        version = manifest.get("version")
        timeout = manifest.get("timeout_seconds")
        maximum_output = manifest.get("maximum_output_bytes")
        mode = manifest.get("binds_verification_mode")
        if (
            set(manifest) != expected
            or manifest.get("schema") != VERIFIER_MANIFEST_SCHEMA
            or manifest.get("verifier_id") != verifier_id
            or type(version) is not str
            or not verifier_id.endswith(f".{version}")
            or type(mode) is not str
            or not mode
            or manifest.get("network") is not False
            or type(timeout) is not int
            or not 1 <= timeout <= MAXIMUM_TIMEOUT_SECONDS
            or type(maximum_output) is not int
            or not 1 <= maximum_output <= MAXIMUM_VERIFIER_OUTPUT_BYTES
        ):
            _fail("AMF_VERIFIER_MANIFEST_INVALID", verifier_id)
        command = manifest.get("command")
        if (
            type(command) is not list
            or not command
            or len(command) > 64
            or any(type(item) is not str or not item or "\x00" in item for item in command)
            or sum(item == "{candidate_path}" for item in command) != 1
            or any(
                item != "{candidate_path}" and ("{" in item or "}" in item)
                for item in command
            )
        ):
            _fail("AMF_VERIFIER_MANIFEST_INVALID", verifier_id)
        working = _relative(
            manifest.get("working_directory"),
            code="AMF_VERIFIER_MANIFEST_INVALID",
            allow_dot=True,
        )
        working_descriptor = _open_relative(
            source_root,
            working,
            code="AMF_VERIFIER_MANIFEST_INVALID",
            directory=True,
        )
        os.close(working_descriptor)
        entrypoint = _relative(command[0], code="AMF_VERIFIER_MANIFEST_INVALID")
        if not entrypoint.endswith(".py") or "{candidate_path}" in entrypoint:
            _fail("AMF_VERIFIER_ENTRYPOINT_INVALID", verifier_id)
        artifacts = manifest.get("source_artifacts")
        if type(artifacts) is not list or not artifacts or len(artifacts) > 256:
            _fail("AMF_VERIFIER_MANIFEST_INVALID", verifier_id)
        selected: list[tuple[str, bytes, str]] = []
        seen: set[str] = set()
        closure: list[dict[str, object]] = []
        for binding in artifacts:
            path, count, digest = _require_binding(
                binding, code="AMF_VERIFIER_SOURCE_PIN_INVALID"
            )
            if path in seen or count > MAXIMUM_SOURCE_ARTIFACT_BYTES:
                _fail("AMF_VERIFIER_SOURCE_PIN_INVALID", verifier_id)
            raw = _read_relative_regular(
                source_root,
                path,
                maximum_bytes=MAXIMUM_SOURCE_ARTIFACT_BYTES,
                code="AMF_VERIFIER_SOURCE_UNAVAILABLE",
            )
            if len(raw) != count or hashlib.sha256(raw).hexdigest() != digest:
                _fail("AMF_VERIFIER_SOURCE_PIN_MISMATCH", path)
            seen.add(path)
            selected.append((path, raw, digest))
            closure.append({"path": path, "bytes": count, "sha256": digest})
        if entrypoint not in seen:
            _fail("AMF_VERIFIER_ENTRYPOINT_NOT_PINNED", verifier_id)
        source_closure_sha256 = hashlib.sha256(canonical_json(closure)).hexdigest()
        return _VerifierDefinition(
            verifier_id=verifier_id,
            verification_mode=mode,
            protocol=protocol,
            registry_manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            manifest_bytes=manifest_bytes,
            command=tuple(command),
            working_directory=working,
            timeout_seconds=timeout,
            maximum_output_bytes=maximum_output,
            source_artifacts=tuple(selected),
            source_closure_sha256=source_closure_sha256,
        )

    def _load_interpreter_identity(self) -> InterpreterIdentity:
        digest, count = _hash_absolute_regular(
            self.python_executable,
            maximum_bytes=MAXIMUM_INTERPRETER_BYTES,
            code="PYTHON_INTERPRETER_INVALID",
        )
        probe = (
            "import json,sys;"
            "print(json.dumps({'implementation':sys.implementation.name,"
            "'version':'.'.join(map(str,sys.version_info[:3]))},"
            "sort_keys=True,separators=(',',':')))"
        )
        try:
            completed = subprocess.run(
                [str(self.python_executable), "-I", "-c", probe],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise VerifierServiceError("PYTHON_INTERPRETER_INVALID") from error
        if completed.returncode != 0 or completed.stderr or len(completed.stdout) > 4_096:
            _fail("PYTHON_INTERPRETER_INVALID")
        value = _strict_object(completed.stdout, code="PYTHON_INTERPRETER_INVALID")
        if set(value) != {"implementation", "version"} or any(
            type(value[key]) is not str or not value[key]
            for key in ("implementation", "version")
        ):
            _fail("PYTHON_INTERPRETER_INVALID")
        return InterpreterIdentity(
            path=str(self.python_executable),
            sha256=digest,
            bytes=count,
            implementation=value["implementation"],
            version=value["version"],
        )

    def _capture_candidate(self, workspace: Path, relative: str) -> CandidateCapture:
        selected = _relative(relative, code="UNSAFE_CANDIDATE_PATH")
        descriptor = _open_relative(
            workspace, selected, code="UNSAFE_CANDIDATE_PATH"
        )
        temporary_fd: int | None = None
        temporary_path: str | None = None
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size <= 0
                or before.st_size > self.maximum_candidate_bytes
            ):
                _fail("CANDIDATE_SIZE_OR_TYPE_INVALID")
            temporary_fd, temporary_path = tempfile.mkstemp(
                prefix=".candidate-capture.", dir=self.artifact_store.data_root
            )
            os.fchmod(temporary_fd, 0o600)
            digest = hashlib.sha256()
            count = 0
            while True:
                chunk = os.read(descriptor, 1_048_576)
                if not chunk:
                    break
                count += len(chunk)
                if count > self.maximum_candidate_bytes:
                    _fail("CANDIDATE_SIZE_OR_TYPE_INVALID")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(temporary_fd, view)
                    if written <= 0:
                        _fail("CANDIDATE_CAPTURE_FAILED")
                    view = view[written:]
            after = os.fstat(descriptor)
            if (
                count != before.st_size
                or _stable_metadata(before) != _stable_metadata(after)
            ):
                _fail("CANDIDATE_CHANGED_DURING_CAPTURE")
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None
            artifact = self.artifact_store.copy_object(
                temporary_path,
                expected_ref=f"artifact/sha256/{digest.hexdigest()}",
            )
            return CandidateCapture(
                workspace_relative_path=selected,
                artifact_ref=artifact.artifact_ref,
                sha256=artifact.sha256,
                bytes=artifact.bytes,
            )
        except ArtifactStoreError as error:
            raise VerifierServiceError("CANDIDATE_CAS_CAPTURE_FAILED", error.code) from error
        finally:
            os.close(descriptor)
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _write_exact(path: Path, raw: bytes, mode: int = 0o400) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            mode,
        )
        try:
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    _fail("VERIFIER_EXECUTION_TREE_FAILED")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _stage_candidate(self, capture: CandidateCapture, destination: Path) -> None:
        artifact = self.artifact_store.resolve(capture.artifact_ref)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            source = os.open(artifact.path, flags)
        except OSError as error:
            raise VerifierServiceError("CANDIDATE_CAS_CORRUPT") from error
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o400,
        )
        digest = hashlib.sha256()
        count = 0
        try:
            before = os.fstat(source)
            while True:
                chunk = os.read(source, 1_048_576)
                if not chunk:
                    break
                count += len(chunk)
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(target, view)
                    if written <= 0:
                        _fail("CANDIDATE_CAS_CORRUPT")
                    view = view[written:]
            after = os.fstat(source)
            os.fsync(target)
        finally:
            os.close(source)
            os.close(target)
        if (
            count != capture.bytes
            or digest.hexdigest() != capture.sha256
            or _stable_metadata(before) != _stable_metadata(after)
        ):
            _fail("CANDIDATE_CAS_CORRUPT")

    @staticmethod
    def _group_alive(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _terminate_group(self, process: subprocess.Popen[bytes]) -> tuple[bool, bool, str | None]:
        group = process.pid
        alive = self._group_alive(group)
        attempted = alive
        if alive:
            try:
                os.killpg(group, signal.SIGTERM)
            except ProcessLookupError:
                alive = False
            except PermissionError:
                return attempted, False, "PROCESS_GROUP_SIGNAL_DENIED"
        deadline = time.monotonic() + self.cleanup_grace_seconds
        while alive and time.monotonic() < deadline:
            process.poll()
            time.sleep(0.01)
            alive = self._group_alive(group)
        if alive:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                alive = False
            except PermissionError:
                return attempted, False, "PROCESS_GROUP_SIGNAL_DENIED"
            deadline = time.monotonic() + self.cleanup_grace_seconds
            while alive and time.monotonic() < deadline:
                process.poll()
                time.sleep(0.01)
                alive = self._group_alive(group)
        return attempted, not alive, None if not alive else "PROCESS_GROUP_SURVIVED"

    def _run_bounded(
        self,
        *,
        command: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
        output_cap: int,
    ) -> _ProcessResult:
        started = time.monotonic_ns()
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            return _ProcessResult(
                "SPAWN_FAILED", None, None, 0, b"", b"", 0, 0, False, True, None
            )
        assert process.stdout is not None and process.stderr is not None
        lock = threading.Lock()
        overflow = threading.Event()
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        observed = {"stdout": 0, "stderr": 0}
        read_failures: list[str] = []

        def reader(label: str, stream: object) -> None:
            try:
                while True:
                    chunk = stream.read(65_536)  # type: ignore[attr-defined]
                    if not chunk:
                        break
                    with lock:
                        observed[label] += len(chunk)
                        retained = len(buffers["stdout"]) + len(buffers["stderr"])
                        room = max(0, output_cap - retained)
                        buffers[label].extend(chunk[:room])
                        if len(chunk) > room:
                            overflow.set()
            except OSError:
                read_failures.append(label)

        threads = [
            threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + timeout_seconds
        state: str | None = None
        cleanup_attempted = False
        cleanup_complete = True
        cleanup_failure: str | None = None
        while process.poll() is None:
            if overflow.is_set():
                state = "OUTPUT_LIMIT_EXCEEDED"
            elif time.monotonic() >= deadline:
                state = "TIMEOUT"
            if state is not None:
                attempted, complete, failure = self._terminate_group(process)
                cleanup_attempted = cleanup_attempted or attempted
                cleanup_complete = cleanup_complete and complete
                cleanup_failure = cleanup_failure or failure
                break
            overflow.wait(0.01)
        try:
            process.wait(timeout=self.cleanup_grace_seconds)
        except subprocess.TimeoutExpired:
            attempted, complete, failure = self._terminate_group(process)
            cleanup_attempted = cleanup_attempted or attempted
            cleanup_complete = cleanup_complete and complete
            cleanup_failure = cleanup_failure or failure or "VERIFIER_LEADER_NOT_REAPED"
        if self._group_alive(process.pid):
            if state is None:
                state = "PROCESS_GROUP_LEAK"
            attempted, complete, failure = self._terminate_group(process)
            cleanup_attempted = cleanup_attempted or attempted
            cleanup_complete = cleanup_complete and complete
            cleanup_failure = cleanup_failure or failure
        for thread in threads:
            thread.join(timeout=self.cleanup_grace_seconds)
        if any(thread.is_alive() for thread in threads):
            cleanup_complete = False
            cleanup_failure = cleanup_failure or "VERIFIER_PIPE_NOT_CLOSED"
            state = "PROCESS_GROUP_CLEANUP_FAILED"
        process.stdout.close()
        process.stderr.close()
        if read_failures:
            state = "OUTPUT_CAPTURE_FAILED"
        if overflow.is_set() and state is None:
            state = "OUTPUT_LIMIT_EXCEEDED"
        if cleanup_failure is not None or not cleanup_complete:
            state = "PROCESS_GROUP_CLEANUP_FAILED"
        if state is None:
            state = "COMPLETED"
        signal_name = None
        if process.returncode is not None and process.returncode < 0:
            try:
                signal_name = signal.Signals(-process.returncode).name
            except ValueError:
                signal_name = f"SIG{-process.returncode}"
        return _ProcessResult(
            state=state,
            exit_code=process.returncode,
            signal_name=signal_name,
            elapsed_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
            stdout_observed_bytes=observed["stdout"],
            stderr_observed_bytes=observed["stderr"],
            cleanup_attempted=cleanup_attempted,
            cleanup_complete=cleanup_complete,
            cleanup_failure=cleanup_failure,
        )

    def _store_bytes(self, raw: bytes) -> ArtifactObject:
        descriptor, path = tempfile.mkstemp(
            prefix=".verifier-output.", dir=self.artifact_store.data_root
        )
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    _fail("VERIFIER_OUTPUT_CAS_FAILED")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            return self.artifact_store.copy_object(path)
        except ArtifactStoreError as error:
            raise VerifierServiceError("VERIFIER_OUTPUT_CAS_FAILED", error.code) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def verify(
        self,
        *,
        session_id: str,
        session_workspace: str | os.PathLike[str],
        target_id: str,
        candidate_relative_path: str,
    ) -> VerificationReceipt:
        """Capture and independently verify one candidate.

        There is intentionally no claimed-verdict argument.  Callers may show
        agent prose in their UI, but only this method's receipt is authoritative.
        """

        if type(session_id) is not str or _ID.fullmatch(session_id) is None:
            _fail("MALFORMED_SESSION_ID")
        binding = self.bindings.get(target_id)
        if binding is None:
            _fail("UNKNOWN_TARGET", str(target_id))
        workspace = Path(session_workspace).expanduser()
        capture = self._capture_candidate(workspace, candidate_relative_path)
        definition = self._source.definitions[binding.verifier_id]

        try:
            current_source = self._load_source_snapshot()
            current_interpreter = self._load_interpreter_identity()
            if current_source != self._source:
                _fail("AMF_SOURCE_IDENTITY_DRIFT")
            if current_interpreter != self._interpreter:
                _fail("PYTHON_INTERPRETER_DRIFT")
        except VerifierServiceError as error:
            process = _ProcessResult(
                "PRECHECK_FAILED", None, None, 0, b"", b"", 0, 0, False, True, None
            )
            return self._receipt(
                session_id=session_id,
                binding=binding,
                capture=capture,
                definition=definition,
                process=process,
                status=VerifierStatus.APPARATUS_ERROR,
                diagnostic_code=error.code,
                verifier_output=None,
            )

        with tempfile.TemporaryDirectory(
            prefix=".pmw-verifier-exec-", dir=self.artifact_store.data_root
        ) as temporary_name:
            temporary = Path(temporary_name)
            source = temporary / "source"
            source.mkdir(mode=0o700)
            for path, raw, _digest in definition.source_artifacts:
                self._write_exact(source.joinpath(*PurePosixPath(path).parts), raw)
            working = (
                source
                if definition.working_directory == "."
                else source.joinpath(*PurePosixPath(definition.working_directory).parts)
            )
            working.mkdir(mode=0o700, parents=True, exist_ok=True)
            candidate_path = temporary / "candidate"
            self._stage_candidate(capture, candidate_path)
            entrypoint = source.joinpath(*PurePosixPath(definition.command[0]).parts)
            arguments = [
                str(candidate_path) if item == "{candidate_path}" else item
                for item in definition.command[1:]
            ]
            command = [
                str(self.python_executable),
                "-I",
                "-c",
                _OFFLINE_BOOTSTRAP,
                str(entrypoint),
                *arguments,
            ]
            home = temporary / "home"
            tmp = temporary / "tmp"
            home.mkdir(mode=0o700)
            tmp.mkdir(mode=0o700)
            process = self._run_bounded(
                command=command,
                cwd=working,
                environment={
                    "HOME": str(home),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin",
                    "PMW_NETWORK": "DENIED",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONHASHSEED": "0",
                    "PYTHONNOUSERSITE": "1",
                    "TMPDIR": str(tmp),
                },
                timeout_seconds=definition.timeout_seconds,
                output_cap=definition.maximum_output_bytes,
            )

        parsed: dict[str, object] | None = None
        status = VerifierStatus.APPARATUS_ERROR
        diagnostic = process.state
        if process.state == "COMPLETED" and not process.stderr:
            try:
                parsed = _strict_object(process.stdout, code="VERIFIER_OUTPUT_INVALID")
                if (
                    parsed.get("schema") != VERIFIER_RESULT_SCHEMA
                    or parsed.get("verifier_id") != definition.verifier_id
                    or type(parsed.get("accepted")) is not bool
                ):
                    _fail("VERIFIER_OUTPUT_INVALID")
                if parsed["accepted"] is True and process.exit_code == 0:
                    status = VerifierStatus.PASS
                    diagnostic = "ACCEPTED"
                elif parsed["accepted"] is False and process.exit_code == 1:
                    status = VerifierStatus.REJECTED
                    diagnostic = "CANDIDATE_REJECTED"
                else:
                    parsed = None
                    diagnostic = "VERIFIER_RESULT_EXIT_MISMATCH"
            except VerifierServiceError as error:
                parsed = None
                diagnostic = error.code
        elif process.state == "COMPLETED" and process.stderr:
            diagnostic = "VERIFIER_STDERR_NONEMPTY"

        try:
            if self._load_source_snapshot() != self._source:
                _fail("AMF_SOURCE_IDENTITY_DRIFT")
            if self._load_interpreter_identity() != self._interpreter:
                _fail("PYTHON_INTERPRETER_DRIFT")
            self.artifact_store.resolve(capture.artifact_ref)
        except (VerifierServiceError, ArtifactStoreError) as error:
            status = VerifierStatus.APPARATUS_ERROR
            parsed = None
            diagnostic = (
                error.code if isinstance(error, (VerifierServiceError, ArtifactStoreError)) else "POSTCHECK_FAILED"
            )
        return self._receipt(
            session_id=session_id,
            binding=binding,
            capture=capture,
            definition=definition,
            process=process,
            status=status,
            diagnostic_code=diagnostic,
            verifier_output=parsed,
        )

    def _receipt(
        self,
        *,
        session_id: str,
        binding: TargetVerifierBinding,
        capture: CandidateCapture,
        definition: _VerifierDefinition,
        process: _ProcessResult,
        status: VerifierStatus,
        diagnostic_code: str,
        verifier_output: dict[str, object] | None,
    ) -> VerificationReceipt:
        stdout = self._store_bytes(process.stdout)
        stderr = self._store_bytes(process.stderr)
        stdout_capture = StreamCapture(
            artifact_ref=stdout.artifact_ref,
            sha256=stdout.sha256,
            retained_bytes=stdout.bytes,
            observed_bytes=process.stdout_observed_bytes,
            truncated=process.stdout_observed_bytes > stdout.bytes,
        )
        stderr_capture = StreamCapture(
            artifact_ref=stderr.artifact_ref,
            sha256=stderr.sha256,
            retained_bytes=stderr.bytes,
            observed_bytes=process.stderr_observed_bytes,
            truncated=process.stderr_observed_bytes > stderr.bytes,
        )
        semantic_command = {
            "python_interpreter_sha256": self._interpreter.sha256,
            "python_isolated": True,
            "offline_bootstrap_sha256": _BOOTSTRAP_SHA256,
            "manifest_command": list(definition.command),
        }
        projected_output = verifier_output
        output_projection = "NONE"
        output_value_sha256: str | None = None
        if verifier_output is not None:
            encoded_output = canonical_json(verifier_output)
            output_value_sha256 = hashlib.sha256(encoded_output).hexdigest()
            if len(encoded_output) <= MAXIMUM_INLINE_VERIFIER_RESULT_BYTES:
                output_projection = "INLINE_COMPLETE"
            else:
                reason_code = verifier_output.get("reason_code")
                projected_output = {
                    "schema": verifier_output.get("schema"),
                    "verifier_id": verifier_output.get("verifier_id"),
                    "accepted": verifier_output.get("accepted"),
                    "reason_code": (
                        reason_code
                        if type(reason_code) is str
                        and len(reason_code.encode("utf-8")) <= 1_024
                        else None
                    ),
                }
                output_projection = "CORE_FIELDS_ONLY_FULL_VALUE_IN_STDOUT_CAS"
        if status is VerifierStatus.PASS:
            claim_ceiling = (
                "The exact captured artifact satisfied the frozen executable predicate. "
                "Novelty and any broader open-problem claim require separate settlement."
            )
        elif status is VerifierStatus.REJECTED:
            claim_ceiling = "The frozen executable predicate rejected this artifact."
        else:
            claim_ceiling = "No verifier claim is authoritative because the apparatus failed."
        core: dict[str, object] = {
            "schema": VERIFIER_RECEIPT_SCHEMA,
            "authority": VERIFIER_AUTHORITY,
            "status": status.value,
            "session_id": session_id,
            "target": {
                "target_id": binding.target_id,
                "target_sha256": binding.target_sha256,
                "verification_mode": binding.verification_mode,
            },
            "candidate": capture.as_dict(),
            "verifier": {
                "verifier_id": definition.verifier_id,
                "protocol": definition.protocol,
                "registry_path": "data/verifiers.json",
                "registry_sha256": self._source.registry_sha256,
                "manifest_path": definition.registry_manifest_path,
                "manifest_sha256": definition.manifest_sha256,
                "manifest_bytes": definition.manifest_bytes,
                "source_closure_sha256": definition.source_closure_sha256,
            },
            "source_tree": {
                "protocol": SOURCE_TREE_PROTOCOL,
                "name": self._source.identity.name,
                "repository": self._source.identity.repository,
                "commit": self._source.identity.commit,
                "git_tree": self._source.identity.git_tree,
                "materializer": {
                    "tree_sha256": self._source.identity.tree_sha256,
                    "manifest_sha256": self._source.identity.manifest_sha256,
                },
            },
            "interpreter": self._interpreter.as_dict(),
            "execution": {
                "command_sha256": hashlib.sha256(canonical_json(semantic_command)).hexdigest(),
                "manifest_network": False,
                "credential_inheritance": False,
                "top_level_python_socket_audit": True,
                "os_network_isolation": False,
                "network_boundary": NETWORK_POLICY,
                "timeout_seconds": definition.timeout_seconds,
                "maximum_output_bytes": definition.maximum_output_bytes,
                "process_state": process.state,
                "exit_code": process.exit_code,
                "signal": process.signal_name,
                "elapsed_ms": process.elapsed_ms,
                "stdout": stdout_capture.as_dict(),
                "stderr": stderr_capture.as_dict(),
                "cleanup_attempted": process.cleanup_attempted,
                "cleanup_complete": process.cleanup_complete,
                "cleanup_failure": process.cleanup_failure,
            },
            "diagnostic_code": diagnostic_code,
            "verifier_output": projected_output,
            "verifier_output_binding": {
                "canonical_value_sha256": output_value_sha256,
                "projection": output_projection,
                "maximum_inline_bytes": MAXIMUM_INLINE_VERIFIER_RESULT_BYTES,
                "full_stdout_artifact_ref": stdout.artifact_ref,
            },
            "claim_ceiling": claim_ceiling,
        }
        receipt_digest = hashlib.sha256(canonical_json(core)).hexdigest()
        document = dict(core)
        document["receipt_ref"] = f"verifier-receipt/sha256/{receipt_digest}"
        return VerificationReceipt(
            status=status,
            receipt_ref=document["receipt_ref"],
            session_id=session_id,
            target_id=binding.target_id,
            candidate=capture,
            verifier_id=definition.verifier_id,
            diagnostic_code=diagnostic_code,
            _document=document,
        )


__all__ = [
    "AmfVerifierService",
    "CandidateCapture",
    "InterpreterIdentity",
    "MAXIMUM_CANDIDATE_BYTES",
    "MAXIMUM_INLINE_VERIFIER_RESULT_BYTES",
    "MAXIMUM_VERIFIER_OUTPUT_BYTES",
    "TargetVerifierBinding",
    "VerificationReceipt",
    "VerifierServiceError",
    "VerifierServiceIdentity",
    "VerifierStatus",
]
