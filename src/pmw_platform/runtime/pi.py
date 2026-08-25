"""Generic Pi RPC adapter for authenticated research sessions.

This module is deliberately independent of the historical ``M0i`` apparatus.
It has no treatment, ballot, target-selection, context-threshold, retry, or
compaction policy.  The host sends one research prompt and waits for Pi's
``agent_settled`` event.  Pi remains responsible for its OAuth refresh and
provider transport; the adapter records the context window reported by Pi's
runtime/model catalog but does not misstate it as an account-route canary or
replace it with a smaller host limit.

The adapter is a trusted transport, not an OS sandbox.  A canonical allowlist
may enable Pi's built-in workspace tools and content-pinned extensions may
expose custom tools.  Built-ins run with the host account's permissions;
extensions remain responsible for any stronger subprocess isolation they
need.  The Pi process itself receives the fixed agent directory so that it can
use and refresh ``auth.json``.  The adapter never deliberately places a
credential value, credential path, or credential-file hash in public identity
or structured receipt metadata.  Bounded raw child frames and stderr remain a
trusted-Pi/redaction boundary: a child that echoes a secret can put it in those
private evidence files and their bounded receipt tails.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePath
import re
import signal
import stat
import time
from typing import Mapping, NoReturn, Protocol

from .contracts import (
    BackendIdentity,
    BackendOutcome,
    BackendStartError,
    MAXIMUM_STOP_GRACE_SECONDS,
    RuntimeContractError,
    SessionRequest,
    StopProof,
)
from .context import ContextWindowControl, ContextWindowPolicy
from .usage import (
    BASIS_RUNTIME_REPORTED_SESSION_TOTALS,
    MAXIMUM_USAGE_REQUEST_RECORDS,
    PROVENANCE_PI_RPC_REPORTED,
    PROVENANCE_PI_RPC_SURFACE_SILENT,
    UsageEvidence,
    UsageRequestRecord,
    UsageTotals,
    observed_count as _observed_count,
    summed_totals as _summed_totals,
)
from ..world.records import canonical_json


PI_BACKEND_CONFIG_SCHEMA = "PMW_PI_RPC_BACKEND_CONFIG_1"
PI_BACKEND_PROTOCOL = "PMW_PI_RPC_1"
PI_PROMPT_PROTOCOL = "PMW_PI_RESEARCH_PROMPT_1"
PI_RPC_DIRECTION_HOST_TO_PI = "HOST_TO_PI"
PI_RPC_DIRECTION_PI_TO_HOST = "PI_TO_HOST"

MAXIMUM_CONFIG_BYTES = 262_144
MAXIMUM_AUTH_BYTES = 1_048_576
MAXIMUM_INPUT_BYTES = 256 * 1024 * 1024
MAXIMUM_RESULT_BYTES = 8 * 1024 * 1024
MAXIMUM_NODE_EXECUTABLE_BYTES = 512 * 1024 * 1024
MAXIMUM_PI_ENTRYPOINT_BYTES = 128 * 1024 * 1024
MAXIMUM_PI_CONFIG_FILE_BYTES = 64 * 1024 * 1024
MAXIMUM_PI_EXTENSION_BYTES = 64 * 1024 * 1024
MAXIMUM_PI_INSTALLATION_BYTES = 512 * 1024 * 1024
MAXIMUM_PI_INSTALLATION_FILE_BYTES = MAXIMUM_PI_INSTALLATION_BYTES
MAXIMUM_PI_INSTALLATION_ENTRIES = 50_000
MAXIMUM_EXTENSIONS = 128
MAXIMUM_TOOLS = 512
MAXIMUM_PI_OBSERVER_EVIDENCE_KEYS = 128
MAXIMUM_PI_OBSERVER_EVIDENCE_BYTES = 1_048_576
_MAXIMUM_JSON_INTEGER = (1 << 63) - 1
_EVENT_QUEUE_CAPACITY = 1_024

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MODEL = re.compile(r"^[^\x00\r\n]{1,512}$")
_TOOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_RESULT_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_THINKING_LEVELS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
)
_BUILTIN_TOOLS = frozenset({"read", "bash", "edit", "write", "grep", "find", "ls"})
_CONFIG_FIELDS = frozenset(
    {
        "schema",
        "name",
        "node_path",
        "pi_entrypoint",
        "pi_agent_dir",
        "provider",
        "model",
        "thinking",
        "auth_kind",
        "account_label",
        "tools",
        "extensions",
        "result_path",
        "limits",
    }
)
_LIMIT_FIELDS = frozenset(
    {
        "maximum_prompt_bytes",
        "maximum_result_bytes",
        "maximum_jsonl_line_bytes",
        "maximum_stdout_bytes",
        "maximum_retained_frame_bytes",
        "maximum_stderr_bytes",
        "maximum_retained_stderr_bytes",
        "maximum_frame_count",
        "response_timeout_seconds",
    }
)
_CONFIG_FILE_NAMES = ("settings.json", "models.json", "models-store.json")
_PUBLIC_ENVIRONMENT_NAMES = (
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "PI_CODING_AGENT_DIR",
    "PI_CODING_AGENT_SESSION_DIR",
    "PI_OFFLINE",
    "PI_SKIP_VERSION_CHECK",
    "PI_TELEMETRY",
    "TMP",
    "TEMP",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
_ACCOUNT_LABEL_DOMAIN = b"PMW_PI_ACCOUNT_LABEL_1\0"
_TOOL_ALLOWLIST_DOMAIN = b"PMW_PI_TOOL_ALLOWLIST_1\0"
_INSTALLATION_TREE_PROTOCOL = "PMW_PI_INSTALLATION_TREE_2"
_INSTALLATION_TREE_DOMAIN = b"PMW_PI_INSTALLATION_TREE_2\0"
_PROMPT_PROTOCOL_BYTES = (
    b"PMW_PI_RESEARCH_PROMPT_1\0briefing-json\0invocation-json\0"
    b"identity-free-backend-outcome\0file-or-final-envelope"
)
_CONTEXT_WINDOW_EXTENSION_NAME = "pi-context-window.mjs"
_CONTEXT_WINDOW_FLAG = "pmw-context-window-tokens"
_ENVELOPE_BEGIN = "PMW_BACKEND_OUTCOME_JSON_BEGIN\n"
_ENVELOPE_END = "\nPMW_BACKEND_OUTCOME_JSON_END"


@dataclass(frozen=True, slots=True)
class PiRpcFrameObservation:
    """One immutable host observation of an exact LF-delimited RPC frame.

    ``ordinal`` is total across both directions for one Pi session.  It and
    both clocks are assigned while the transport's single observation lock is
    held.  The observer never participates in frame parsing or correlation and
    has no return channel through which it could replace transport bytes.
    """

    direction: str
    raw_lf_json: bytes
    ordinal: int
    observed_at: str
    monotonic_ns: int


@dataclass(frozen=True, slots=True)
class PiRpcObserverFinality:
    """Typed terminal context delivered only after Pi process finality.

    ``backend_outcome`` is the already bounded and schema-validated PMW
    object.  It remains agent-authored candidate material, never proof
    authority.  Supplying it after the stop proof lets a host observer perform
    post-session verification before it freezes its evidence, without creating
    a feedback/control channel into Pi.
    """

    backend_success: bool
    terminal_reason: str
    stop_proof: StopProof
    observation_count: int
    transport_evidence: Mapping[str, object]
    backend_outcome: BackendOutcome | None = None


class PiRpcObserver(Protocol):
    """Host-owned sink for one Pi session's ordered transport frames."""

    async def observe(self, observation: PiRpcFrameObservation) -> None: ...

    async def finalize(
        self, finality: PiRpcObserverFinality
    ) -> Mapping[str, object] | None: ...


class PiRpcObserverFactory(Protocol):
    """Create session-local observers under one public launch identity.

    ``identity`` uses the existing bounded, secret-rejecting public identity
    contract.  ``evidence_keys`` must be a sorted, unique tuple and is bound
    into the Pi backend identity before any session starts.
    """

    @property
    def identity(self) -> BackendIdentity: ...

    @property
    def evidence_keys(self) -> tuple[str, ...]: ...

    def create(self, request: SessionRequest) -> PiRpcObserver: ...


class PiBackendError(ValueError):
    """A stable configuration, transport, or result-validation failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


class PiRpcFailure(RuntimeError):
    """A stable failure in the LF-delimited Pi RPC transport."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _observer_factory_contract(
    factory: PiRpcObserverFactory,
) -> tuple[BackendIdentity, tuple[str, ...]]:
    try:
        identity = factory.identity
        evidence_keys = factory.evidence_keys
    except Exception as error:  # noqa: BLE001
        raise PiBackendError(
            "PI_OBSERVER_FACTORY_INVALID", "identity unavailable"
        ) from error
    if not isinstance(identity, BackendIdentity):
        raise PiBackendError("PI_OBSERVER_FACTORY_INVALID", "identity")
    if (
        not isinstance(evidence_keys, tuple)
        or len(evidence_keys) > MAXIMUM_PI_OBSERVER_EVIDENCE_KEYS
        or any(
            type(key) is not str or _RESULT_COMPONENT.fullmatch(key) is None
            for key in evidence_keys
        )
        or evidence_keys != tuple(sorted(set(evidence_keys)))
        or "pi_rpc" in evidence_keys
    ):
        raise PiBackendError("PI_OBSERVER_FACTORY_INVALID", "evidence_keys")
    return identity, evidence_keys


def _fail(code: str, detail: str = "") -> NoReturn:
    raise PiBackendError(code, detail)


def _duplicate_rejecting_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _bounded_integer(encoded: str) -> int:
    value = int(encoded)
    if not -_MAXIMUM_JSON_INTEGER <= value <= _MAXIMUM_JSON_INTEGER:
        raise ValueError("integer out of bounds")
    return value


def _reject_number(_encoded: str) -> NoReturn:
    raise ValueError("floating-point JSON is unsupported")


def _parse_json(raw: bytes, *, label: str) -> object:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_int=_bounded_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise PiBackendError("MALFORMED_PI_JSON", label) from error
    return value


def _open_readonly_nofollow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _read_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    code: str,
) -> bytes:
    if not path.is_absolute():
        _fail(code, "path must be absolute")
    try:
        descriptor = _open_readonly_nofollow(path)
    except OSError as error:
        raise PiBackendError(code, "open failed") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(code, "not a regular file")
        remaining = maximum_bytes + 1
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum_bytes:
            _fail(code, "file is too large")
        return raw
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class _FilePin:
    path: Path = field(repr=False)
    sha256: str
    identity: tuple[int, int, int, int, int] = field(repr=False)
    maximum_bytes: int = field(repr=False)
    executable: bool = field(default=False, repr=False)

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        maximum_bytes: int,
        executable: bool = False,
    ) -> "_FilePin":
        if type(maximum_bytes) is not int or maximum_bytes < 0:
            raise ValueError("maximum_bytes must be a non-negative integer")
        if not path.is_absolute():
            _fail("PI_RUNTIME_FILE_INVALID", "path must be absolute")
        try:
            metadata = path.lstat()
        except OSError as error:
            raise PiBackendError("PI_RUNTIME_FILE_INVALID", path.name) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            _fail("PI_RUNTIME_FILE_INVALID", path.name)
        if metadata.st_size > maximum_bytes:
            _fail("PI_RUNTIME_FILE_INVALID", f"{path.name}: byte limit")
        if executable and metadata.st_mode & 0o111 == 0:
            _fail("PI_RUNTIME_FILE_INVALID", f"{path.name}: not executable")
        digest = hashlib.sha256()
        try:
            descriptor = _open_readonly_nofollow(path)
        except OSError as error:
            raise PiBackendError("PI_RUNTIME_FILE_INVALID", path.name) from error
        try:
            opened_before = os.fstat(descriptor)
            if not stat.S_ISREG(opened_before.st_mode):
                _fail("PI_RUNTIME_FILE_INVALID", path.name)
            if _metadata_identity(opened_before) != _metadata_identity(metadata):
                _fail("PI_RUNTIME_FILE_DRIFT", path.name)
            if opened_before.st_size > maximum_bytes:
                _fail("PI_RUNTIME_FILE_INVALID", f"{path.name}: byte limit")
            if executable and opened_before.st_mode & 0o111 == 0:
                _fail("PI_RUNTIME_FILE_INVALID", f"{path.name}: not executable")

            bytes_read = 0
            while bytes_read < opened_before.st_size:
                chunk = os.read(
                    descriptor,
                    min(1024 * 1024, opened_before.st_size - bytes_read),
                )
                if not chunk:
                    _fail("PI_RUNTIME_FILE_DRIFT", f"{path.name}: short read")
                digest.update(chunk)
                bytes_read += len(chunk)
            if opened_before.st_size < maximum_bytes and os.read(descriptor, 1):
                _fail("PI_RUNTIME_FILE_DRIFT", f"{path.name}: grew while read")
            opened_after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            final_metadata = path.lstat()
        except OSError as error:
            raise PiBackendError("PI_RUNTIME_FILE_DRIFT", path.name) from error
        if (
            stat.S_ISLNK(final_metadata.st_mode)
            or not stat.S_ISREG(final_metadata.st_mode)
        ):
            _fail("PI_RUNTIME_FILE_DRIFT", path.name)
        identity = _metadata_identity(opened_before)
        if (
            bytes_read != opened_before.st_size
            or _metadata_identity(opened_after) != identity
            or _metadata_identity(final_metadata) != identity
            or (executable and final_metadata.st_mode & 0o111 == 0)
        ):
            _fail("PI_RUNTIME_FILE_DRIFT", path.name)
        return cls(
            path=path,
            sha256=digest.hexdigest(),
            identity=identity,
            maximum_bytes=maximum_bytes,
            executable=executable,
        )

    def verify(self) -> None:
        selected = _FilePin.create(
            self.path,
            maximum_bytes=self.maximum_bytes,
            executable=self.executable,
        )
        if selected.sha256 != self.sha256 or selected.identity != self.identity:
            _fail("PI_RUNTIME_FILE_DRIFT", self.path.name)


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _tree_digest(root: Path) -> str:
    if not root.is_absolute():
        _fail("PI_INSTALLATION_TREE_INVALID", "path must be absolute")
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise PiBackendError("PI_INSTALLATION_TREE_INVALID", "unavailable") from error
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise PiBackendError("PI_INSTALLATION_TREE_INVALID", "unavailable") from error
    if (
        root.is_symlink()
        or not stat.S_ISDIR(root_metadata.st_mode)
        or resolved_root != root
    ):
        _fail("PI_INSTALLATION_TREE_INVALID", "not an exact directory")

    entries: list[tuple[Path, str, os.stat_result]] = []

    def add_entry(path: Path, kind: str, metadata: os.stat_result) -> None:
        entries.append((path, kind, metadata))
        if len(entries) > MAXIMUM_PI_INSTALLATION_ENTRIES:
            _fail("PI_INSTALLATION_TREE_INVALID", "entry limit")

    def verify_directory(
        path: Path,
        expected: os.stat_result,
        *,
        label: str,
    ) -> None:
        try:
            current = path.lstat()
        except OSError as error:
            raise PiBackendError(
                "PI_INSTALLATION_TREE_INVALID", f"directory drift: {label}"
            ) from error
        if (
            not stat.S_ISDIR(current.st_mode)
            or _metadata_identity(current) != _metadata_identity(expected)
        ):
            _fail("PI_INSTALLATION_TREE_INVALID", f"directory drift: {label}")

    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        names.sort()
        files.sort()
        traversable_names: list[str] = []
        for name in names:
            selected = directory_path / name
            try:
                metadata = selected.lstat()
            except OSError as error:
                raise PiBackendError("PI_INSTALLATION_TREE_INVALID", name) from error
            if stat.S_ISLNK(metadata.st_mode):
                add_entry(selected, "symlink", metadata)
            elif stat.S_ISDIR(metadata.st_mode):
                add_entry(selected, "directory", metadata)
                traversable_names.append(name)
            else:
                _fail("PI_INSTALLATION_TREE_INVALID", f"special directory: {name}")
        names[:] = traversable_names
        for name in files:
            selected = directory_path / name
            try:
                metadata = selected.lstat()
            except OSError as error:
                raise PiBackendError("PI_INSTALLATION_TREE_INVALID", name) from error
            if stat.S_ISREG(metadata.st_mode):
                add_entry(selected, "file", metadata)
            elif stat.S_ISLNK(metadata.st_mode):
                add_entry(selected, "symlink", metadata)
            else:
                _fail("PI_INSTALLATION_TREE_INVALID", f"special file: {name}")

    total_before_hash = sum(
        metadata.st_size
        for _path, kind, metadata in entries
        if kind == "file"
    )
    if total_before_hash > MAXIMUM_PI_INSTALLATION_BYTES:
        _fail("PI_INSTALLATION_TREE_INVALID", "byte limit")

    digest = hashlib.sha256(_INSTALLATION_TREE_DOMAIN)
    digest.update(len(entries).to_bytes(8, "big"))
    total = 0
    for path, kind, scanned_metadata in sorted(
        entries, key=lambda value: value[0].relative_to(root).as_posix()
    ):
        relative = os.fsencode(path.relative_to(root).as_posix())
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        if kind == "directory":
            verify_directory(path, scanned_metadata, label=path.name)
            digest.update(b"D")
            continue
        if kind == "file":
            pin = _FilePin.create(
                path,
                maximum_bytes=min(
                    MAXIMUM_PI_INSTALLATION_FILE_BYTES,
                    MAXIMUM_PI_INSTALLATION_BYTES - total,
                ),
            )
            if pin.identity != _metadata_identity(scanned_metadata):
                _fail("PI_INSTALLATION_TREE_INVALID", f"file drift: {path.name}")
            size = pin.identity[2]
            total += size
            if total > MAXIMUM_PI_INSTALLATION_BYTES:
                _fail("PI_INSTALLATION_TREE_INVALID", "byte limit")
            digest.update(b"F")
            digest.update(size.to_bytes(8, "big"))
            digest.update(bytes.fromhex(pin.sha256))
            continue
        try:
            target = os.readlink(path)
            target_bytes = os.fsencode(target)
            resolved_target = (path.parent / target).resolve(strict=True)
            resolved_target.relative_to(root)
        except (OSError, ValueError) as error:
            raise PiBackendError(
                "PI_INSTALLATION_TREE_INVALID", f"unsafe symlink: {path.name}"
            ) from error
        if os.path.isabs(target) or len(target_bytes) > 16_384:
            _fail("PI_INSTALLATION_TREE_INVALID", f"unsafe symlink: {path.name}")
        try:
            current = path.lstat()
        except OSError as error:
            raise PiBackendError(
                "PI_INSTALLATION_TREE_INVALID", f"symlink drift: {path.name}"
            ) from error
        if (
            not stat.S_ISLNK(current.st_mode)
            or _metadata_identity(current) != _metadata_identity(scanned_metadata)
        ):
            _fail("PI_INSTALLATION_TREE_INVALID", f"symlink drift: {path.name}")
        digest.update(b"L")
        digest.update(len(target_bytes).to_bytes(4, "big"))
        digest.update(target_bytes)
    if not entries:
        _fail("PI_INSTALLATION_TREE_INVALID", "empty")
    # Recheck every scanned directory only after all descendants have been
    # hashed.  A nested add/remove changes its directory metadata even when it
    # happens after that directory's manifest row was first processed.
    verify_directory(root, root_metadata, label="root")
    for path, kind, scanned_metadata in entries:
        if kind == "directory":
            verify_directory(path, scanned_metadata, label=path.name)
    return digest.hexdigest()


def _require_exact_directory(path: Path, *, code: str, label: str) -> Path:
    if not path.is_absolute():
        _fail(code, f"{label}: not absolute")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PiBackendError(code, label) from error
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or resolved != path:
        _fail(code, f"{label}: not exact")
    return path


def _validate_agent_directory(path: Path) -> None:
    selected = _require_exact_directory(
        path, code="PI_AUTH_BOUNDARY_INVALID", label="agent directory"
    )
    metadata = selected.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        _fail("PI_AUTH_BOUNDARY_INVALID", "agent directory ownership/mode")


def _validate_oauth_boundary(agent_dir: Path, provider: str) -> None:
    """Check only boundary metadata and the selected credential's kind.

    The OAuth value is intentionally neither returned nor hashed.  Pi is free
    to refresh ``auth.json`` after this check.
    """

    _validate_agent_directory(agent_dir)
    auth_path = agent_dir / "auth.json"
    try:
        metadata = auth_path.lstat()
    except OSError as error:
        raise PiBackendError("PI_AUTH_BOUNDARY_INVALID", "auth unavailable") from error
    if (
        auth_path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        _fail("PI_AUTH_BOUNDARY_INVALID", "auth ownership/mode/type")
    raw = _read_regular_file(
        auth_path,
        maximum_bytes=MAXIMUM_AUTH_BYTES,
        code="PI_AUTH_BOUNDARY_INVALID",
    )
    value = _parse_json(raw, label="auth")
    credential = value.get(provider) if type(value) is dict else None
    if type(credential) is not dict or credential.get("type") != "oauth":
        _fail("PI_AUTH_KIND_MISMATCH", provider)


def _positive_integer(
    value: object,
    *,
    label: str,
    maximum: int,
) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        _fail("MALFORMED_PI_CONFIG", label)
    return value


def _identifier(value: object, *, label: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail("MALFORMED_PI_CONFIG", label)
    return value


def _absolute_path(value: object, *, label: str) -> Path:
    if type(value) is not str or not value or "\x00" in value:
        _fail("MALFORMED_PI_CONFIG", label)
    selected = Path(value)
    if not selected.is_absolute():
        _fail("MALFORMED_PI_CONFIG", label)
    return selected


def _validate_result_path(value: object) -> str:
    if type(value) is not str or len(value.encode("utf-8")) > 128:
        _fail("MALFORMED_PI_CONFIG", "result_path")
    selected = PurePath(value)
    if (
        selected.is_absolute()
        or len(selected.parts) != 1
        or _RESULT_COMPONENT.fullmatch(value) is None
    ):
        _fail("MALFORMED_PI_CONFIG", "result_path")
    return value


@dataclass(frozen=True, slots=True)
class PiBackendConfig:
    """A canonical Pi RPC configuration with redacted public identity."""

    name: str
    node_path: Path = field(repr=False)
    pi_entrypoint: Path = field(repr=False)
    pi_agent_dir: Path = field(repr=False)
    provider: str
    model: str
    thinking: str
    account_label_sha256: str
    tools: tuple[str, ...]
    extensions: tuple[_FilePin, ...] = field(repr=False)
    result_path: str
    maximum_prompt_bytes: int
    maximum_result_bytes: int
    maximum_jsonl_line_bytes: int
    maximum_stdout_bytes: int
    maximum_retained_frame_bytes: int
    maximum_stderr_bytes: int
    maximum_retained_stderr_bytes: int
    maximum_frame_count: int
    response_timeout_seconds: int
    node_pin: _FilePin = field(repr=False)
    entrypoint_pin: _FilePin = field(repr=False)
    installation_tree_sha256: str
    config_file_pins: tuple[tuple[str, _FilePin | None], ...] = field(repr=False)
    context_window_extension: _FilePin = field(repr=False)

    @classmethod
    def from_value(cls, value: object) -> "PiBackendConfig":
        if type(value) is not dict or set(value) != _CONFIG_FIELDS:
            _fail("MALFORMED_PI_CONFIG", "fields")
        if value.get("schema") != PI_BACKEND_CONFIG_SCHEMA:
            _fail("MALFORMED_PI_CONFIG", "schema")
        name = _identifier(value.get("name"), label="name", pattern=_NAME)
        provider = _identifier(
            value.get("provider"), label="provider", pattern=_NAME
        )
        model = _identifier(value.get("model"), label="model", pattern=_MODEL)
        thinking = value.get("thinking")
        if thinking not in _THINKING_LEVELS:
            _fail("MALFORMED_PI_CONFIG", "thinking")
        if value.get("auth_kind") != "oauth":
            _fail("MALFORMED_PI_CONFIG", "auth_kind")
        account_label = value.get("account_label")
        if (
            type(account_label) is not str
            or not account_label
            or len(account_label.encode("utf-8", errors="strict")) > 1_024
            or "\x00" in account_label
        ):
            _fail("MALFORMED_PI_CONFIG", "account_label")
        account_label_sha256 = hashlib.sha256(
            _ACCOUNT_LABEL_DOMAIN
            + provider.encode("utf-8")
            + b"\0"
            + account_label.encode("utf-8")
        ).hexdigest()

        node_path = _absolute_path(value.get("node_path"), label="node_path")
        entrypoint = _absolute_path(
            value.get("pi_entrypoint"), label="pi_entrypoint"
        )
        agent_dir = _absolute_path(value.get("pi_agent_dir"), label="pi_agent_dir")
        if entrypoint.name != "cli.js" or entrypoint.parent.name != "dist":
            _fail("MALFORMED_PI_CONFIG", "pi_entrypoint must be dist/cli.js")
        node_pin = _FilePin.create(
            node_path,
            maximum_bytes=MAXIMUM_NODE_EXECUTABLE_BYTES,
            executable=True,
        )
        entrypoint_pin = _FilePin.create(
            entrypoint,
            maximum_bytes=MAXIMUM_PI_ENTRYPOINT_BYTES,
        )
        installation_tree_sha256 = _tree_digest(entrypoint.parent.parent)
        _validate_oauth_boundary(agent_dir, provider)

        raw_tools = value.get("tools")
        if type(raw_tools) is not list or len(raw_tools) > MAXIMUM_TOOLS:
            _fail("MALFORMED_PI_CONFIG", "tools")
        tools = tuple(
            _identifier(item, label="tool", pattern=_TOOL) for item in raw_tools
        )
        if tools != tuple(sorted(set(tools))):
            _fail("MALFORMED_PI_CONFIG", "tools must be sorted and unique")
        raw_extensions = value.get("extensions")
        if type(raw_extensions) is not list or len(raw_extensions) > MAXIMUM_EXTENSIONS:
            _fail("MALFORMED_PI_CONFIG", "extensions")
        extension_paths = tuple(
            _absolute_path(item, label="extension") for item in raw_extensions
        )
        if tuple(str(item) for item in extension_paths) != tuple(
            sorted({str(item) for item in extension_paths})
        ):
            _fail("MALFORMED_PI_CONFIG", "extensions must be sorted and unique")
        extensions = tuple(
            _FilePin.create(path, maximum_bytes=MAXIMUM_PI_EXTENSION_BYTES)
            for path in extension_paths
        )
        custom_tools = set(tools).difference(_BUILTIN_TOOLS)
        if custom_tools and not extensions:
            _fail(
                "MALFORMED_PI_CONFIG",
                "custom tools require explicit extensions",
            )

        result_path = _validate_result_path(value.get("result_path"))
        limits = value.get("limits")
        if type(limits) is not dict or set(limits) != _LIMIT_FIELDS:
            _fail("MALFORMED_PI_CONFIG", "limits")
        maximum_prompt_bytes = _positive_integer(
            limits.get("maximum_prompt_bytes"),
            label="maximum_prompt_bytes",
            maximum=MAXIMUM_INPUT_BYTES,
        )
        maximum_result_bytes = _positive_integer(
            limits.get("maximum_result_bytes"),
            label="maximum_result_bytes",
            maximum=MAXIMUM_RESULT_BYTES,
        )
        maximum_jsonl_line_bytes = _positive_integer(
            limits.get("maximum_jsonl_line_bytes"),
            label="maximum_jsonl_line_bytes",
            maximum=64 * 1024 * 1024,
        )
        maximum_stdout_bytes = _positive_integer(
            limits.get("maximum_stdout_bytes"),
            label="maximum_stdout_bytes",
            maximum=1 << 40,
        )
        maximum_retained_frame_bytes = _positive_integer(
            limits.get("maximum_retained_frame_bytes"),
            label="maximum_retained_frame_bytes",
            maximum=1 << 30,
        )
        maximum_stderr_bytes = _positive_integer(
            limits.get("maximum_stderr_bytes"),
            label="maximum_stderr_bytes",
            maximum=1 << 40,
        )
        maximum_retained_stderr_bytes = _positive_integer(
            limits.get("maximum_retained_stderr_bytes"),
            label="maximum_retained_stderr_bytes",
            maximum=1 << 30,
        )
        if maximum_retained_frame_bytes > maximum_stdout_bytes:
            _fail("MALFORMED_PI_CONFIG", "retained frames exceed observed stdout")
        if maximum_retained_stderr_bytes > maximum_stderr_bytes:
            _fail("MALFORMED_PI_CONFIG", "retained stderr exceeds observed stderr")
        maximum_frame_count = _positive_integer(
            limits.get("maximum_frame_count"),
            label="maximum_frame_count",
            maximum=10_000_000,
        )
        response_timeout_seconds = _positive_integer(
            limits.get("response_timeout_seconds"),
            label="response_timeout_seconds",
            maximum=3_600,
        )
        config_file_pins: list[tuple[str, _FilePin | None]] = []
        for filename in _CONFIG_FILE_NAMES:
            path = agent_dir / filename
            if path.exists() or path.is_symlink():
                config_file_pins.append(
                    (
                        filename,
                        _FilePin.create(
                            path,
                            maximum_bytes=MAXIMUM_PI_CONFIG_FILE_BYTES,
                        ),
                    )
                )
            else:
                config_file_pins.append((filename, None))
        context_window_extension = _FilePin.create(
            Path(__file__).resolve().with_name(_CONTEXT_WINDOW_EXTENSION_NAME),
            maximum_bytes=MAXIMUM_PI_EXTENSION_BYTES,
        )
        return cls(
            name=name,
            node_path=node_path,
            pi_entrypoint=entrypoint,
            pi_agent_dir=agent_dir,
            provider=provider,
            model=model,
            thinking=thinking,  # type: ignore[arg-type]
            account_label_sha256=account_label_sha256,
            tools=tools,
            extensions=extensions,
            result_path=result_path,
            maximum_prompt_bytes=maximum_prompt_bytes,
            maximum_result_bytes=maximum_result_bytes,
            maximum_jsonl_line_bytes=maximum_jsonl_line_bytes,
            maximum_stdout_bytes=maximum_stdout_bytes,
            maximum_retained_frame_bytes=maximum_retained_frame_bytes,
            maximum_stderr_bytes=maximum_stderr_bytes,
            maximum_retained_stderr_bytes=maximum_retained_stderr_bytes,
            maximum_frame_count=maximum_frame_count,
            response_timeout_seconds=response_timeout_seconds,
            node_pin=node_pin,
            entrypoint_pin=entrypoint_pin,
            installation_tree_sha256=installation_tree_sha256,
            config_file_pins=tuple(config_file_pins),
            context_window_extension=context_window_extension,
        )

    def to_public_value(self) -> dict[str, object]:
        tools_bytes = canonical_json(list(self.tools))
        builtin_tools = sorted(_BUILTIN_TOOLS.intersection(self.tools))
        custom_tools = sorted(set(self.tools).difference(_BUILTIN_TOOLS))
        return {
            "schema": PI_BACKEND_CONFIG_SCHEMA,
            "provider": self.provider,
            "model": self.model,
            "thinking": self.thinking,
            "auth_kind": "oauth",
            "account_label_sha256": self.account_label_sha256,
            "node_sha256": self.node_pin.sha256,
            "pi_cli_sha256": self.entrypoint_pin.sha256,
            "pi_installation_tree_protocol": _INSTALLATION_TREE_PROTOCOL,
            "pi_installation_tree_sha256": self.installation_tree_sha256,
            "runtime_pin_limits": {
                "maximum_node_executable_bytes": MAXIMUM_NODE_EXECUTABLE_BYTES,
                "maximum_pi_entrypoint_bytes": MAXIMUM_PI_ENTRYPOINT_BYTES,
                "maximum_pi_config_file_bytes": MAXIMUM_PI_CONFIG_FILE_BYTES,
                "maximum_pi_extension_bytes": MAXIMUM_PI_EXTENSION_BYTES,
                "maximum_pi_installation_file_bytes": (
                    MAXIMUM_PI_INSTALLATION_FILE_BYTES
                ),
                "maximum_pi_installation_bytes": MAXIMUM_PI_INSTALLATION_BYTES,
                "maximum_pi_installation_entries": MAXIMUM_PI_INSTALLATION_ENTRIES,
            },
            "pi_config_files": [
                {
                    "name": name,
                    "sha256": None if pin is None else pin.sha256,
                }
                for name, pin in self.config_file_pins
            ],
            "prompt_protocol": PI_PROMPT_PROTOCOL,
            "prompt_protocol_sha256": hashlib.sha256(
                _PROMPT_PROTOCOL_BYTES
            ).hexdigest(),
            "tools": list(self.tools),
            "tool_allowlist_sha256": hashlib.sha256(
                _TOOL_ALLOWLIST_DOMAIN + tools_bytes
            ).hexdigest(),
            "extensions": [
                {
                    "ordinal": ordinal,
                    "filename": pin.path.name,
                    "sha256": pin.sha256,
                }
                for ordinal, pin in enumerate(self.extensions)
            ],
            "result_path": self.result_path,
            "result_schema": "PMW_RUNTIME_BACKEND_OUTCOME_1",
            "limits": {
                "maximum_prompt_bytes": self.maximum_prompt_bytes,
                "maximum_result_bytes": self.maximum_result_bytes,
                "maximum_jsonl_line_bytes": self.maximum_jsonl_line_bytes,
                "maximum_stdout_bytes": self.maximum_stdout_bytes,
                "maximum_retained_frame_bytes": self.maximum_retained_frame_bytes,
                "maximum_stderr_bytes": self.maximum_stderr_bytes,
                "maximum_retained_stderr_bytes": self.maximum_retained_stderr_bytes,
                "maximum_frame_count": self.maximum_frame_count,
                "response_timeout_seconds": self.response_timeout_seconds,
            },
            "environment_names": list(_PUBLIC_ENVIRONMENT_NAMES),
            "context_window_control": ContextWindowControl.NATIVE_MODEL_WINDOW.value,
            "context_window_semantics": (
                "PI_NATIVE_BUDGETING_COMPACTION_AND_OVERFLOW_NOT_STRICT_INPUT_GATE"
            ),
            "context_window_extension": {
                "filename": self.context_window_extension.path.name,
                "sha256": self.context_window_extension.sha256,
                "flag": _CONTEXT_WINDOW_FLAG,
            },
            "host_prompt_count": 1,
            "host_retry_count": 0,
            "host_compaction_count": 0,
            "pi_retry_compaction_policy": "PINNED_PI_CONFIG_NOT_HOST_OVERRIDDEN",
            "requested_builtin_names": builtin_tools,
            "requested_extension_tool_names": custom_tools,
            "tool_resolution": (
                "PINNED_PI_BUILTIN_REGISTRY_THEN_PINNED_EXTENSION_REGISTRATION_"
                "SAME_NAME_EXTENSION_WINS"
            ),
            "tool_security_boundary": (
                "COOPERATIVE_HOST_ACCOUNT_ACCESS_NOT_OS_SANDBOX"
            ),
            "containment": "COOPERATIVE_PROCESS_GROUP",
            "runtime_pin_strategy": "PATH_PIN_RECHECK_NOT_IMMUTABLE_SNAPSHOT",
        }

    def verify_runtime(self) -> None:
        self.node_pin.verify()
        self.entrypoint_pin.verify()
        if (
            _tree_digest(self.pi_entrypoint.parent.parent)
            != self.installation_tree_sha256
        ):
            _fail("PI_RUNTIME_FILE_DRIFT", "installation tree")
        for name, pin in self.config_file_pins:
            path = self.pi_agent_dir / name
            if pin is None:
                if path.exists() or path.is_symlink():
                    _fail("PI_RUNTIME_FILE_DRIFT", name)
            else:
                pin.verify()
        for pin in self.extensions:
            pin.verify()
        self.context_window_extension.verify()
        _validate_oauth_boundary(self.pi_agent_dir, self.provider)


def load_pi_backend_config(path: Path) -> PiBackendConfig:
    """Load a strict bounded JSON Pi backend configuration."""

    if not isinstance(path, Path):
        raise TypeError("path must be Path")
    selected = path if path.is_absolute() else path.absolute()
    raw = _read_regular_file(
        selected,
        maximum_bytes=MAXIMUM_CONFIG_BYTES,
        code="PI_CONFIG_UNREADABLE",
    )
    return PiBackendConfig.from_value(_parse_json(raw, label="config"))


def _require_session_layout(request: SessionRequest) -> None:
    roots = {
        label: _require_exact_directory(
            getattr(request, label), code="PI_SESSION_LAYOUT_INVALID", label=label
        )
        for label in ("private_root", "workspace", "cache", "evidence")
    }
    rows = list(roots.items())
    for index, (left_name, left) in enumerate(rows):
        for right_name, right in rows[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                _fail(
                    "PI_SESSION_LAYOUT_INVALID", f"{left_name}/{right_name} overlap"
                )


def _mkdir_private(root: Path, name: str) -> Path:
    selected = root / name
    selected.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(selected, 0o700)
    return _require_exact_directory(
        selected, code="PI_SESSION_LAYOUT_INVALID", label=name
    )


def _session_environment(
    config: PiBackendConfig,
    request: SessionRequest,
    *,
    session_dir: Path,
) -> dict[str, str]:
    home = _mkdir_private(request.private_root, "pi-home")
    temporary = _mkdir_private(request.private_root, "pi-tmp")
    xdg_config = _mkdir_private(request.private_root, "pi-xdg-config")
    xdg_data = _mkdir_private(request.private_root, "pi-xdg-data")
    xdg_state = _mkdir_private(request.private_root, "pi-xdg-state")
    xdg_cache = _mkdir_private(request.cache, "pi-xdg-cache")
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_STATE_HOME": str(xdg_state),
        "PI_CODING_AGENT_DIR": str(config.pi_agent_dir),
        "PI_CODING_AGENT_SESSION_DIR": str(session_dir),
        "PI_OFFLINE": "1",
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_TELEMETRY": "0",
    }


def _build_argv(
    config: PiBackendConfig,
    request: SessionRequest,
    session_dir: Path,
) -> tuple[str, ...]:
    extension_args = tuple(
        item
        for pin in config.extensions
        for item in ("--extension", str(pin.path))
    )
    # ``--no-builtin-tools`` alone leaves Pi's extension-tool allowlist
    # undefined.  A loaded extension could then activate a registered custom
    # tool after launch.  ``--no-tools`` is the exact empty allowlist.
    tool_args = (
        ("--tools", ",".join(config.tools))
        if config.tools
        else ("--no-tools",)
    )
    builtin_tool_args = (
        ()
        if _BUILTIN_TOOLS.intersection(config.tools)
        else ("--no-builtin-tools",)
    )
    context_args = (
        ()
        if request.context_window_tokens is None
        else (
            f"--{_CONTEXT_WINDOW_FLAG}",
            str(request.context_window_tokens),
        )
    )
    return (
        str(config.node_path),
        str(config.pi_entrypoint),
        "--mode",
        "rpc",
        "--name",
        f"pmw-{request.spec.session_id}",
        "--session-dir",
        str(session_dir),
        "--provider",
        config.provider,
        "--model",
        config.model,
        "--thinking",
        config.thinking,
        *builtin_tool_args,
        *tool_args,
        "--no-extensions",
        *extension_args,
        "--extension",
        str(config.context_window_extension.path),
        *context_args,
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-approve",
    )


def _read_prompt_input(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    raw = _read_regular_file(
        path, maximum_bytes=maximum_bytes, code="PI_PROMPT_INPUT_INVALID"
    )
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise PiBackendError("PI_PROMPT_INPUT_INVALID", label) from error
    return raw


def _build_prompt(
    config: PiBackendConfig,
    request: SessionRequest,
    result_path: Path,
) -> tuple[str, dict[str, object]]:
    briefing = _read_prompt_input(
        request.briefing_path,
        maximum_bytes=config.maximum_prompt_bytes,
        label="briefing",
    )
    invocation = _read_prompt_input(
        request.invocation_path,
        maximum_bytes=config.maximum_prompt_bytes,
        label="invocation",
    )
    header = (
        f"{PI_PROMPT_PROTOCOL}\n"
        "You are one independent mathematical research session. Work on the "
        "host-authenticated research world described below. Read the full current "
        "mathematical state, choose a valuable route, test objections, and leave a "
        "concise, checkable contribution. Do not inspect, copy, or report credentials.\n\n"
        "Runtime limits and context policy come only from HOST_INVOCATION_JSON and "
        "the immutable launch. Any campaign budgets, phases, steers, or tool limits "
        "mentioned in historical world records, including every omitted predecessor "
        "problem-card `budget_contract`, are non-operative provenance.\n\n"
        "HOST_INVOCATION_JSON also carries a `verifier_kit` record. When it reports "
        "`available: true`, a read-only verifier kit exists in this workspace and "
        "its `invocation` block states the exact command, accepted arguments, exit "
        "codes and evidence paths. Kit verdicts are advisory in-session evidence; "
        "the host verifies independently after settlement. The host neither "
        "requires nor recommends using it, and using it is not a success "
        "criterion.\n\n"
        "The host, not you, owns world/cohort/session identity and final admission. "
        "Any proposed contribution must therefore use the identity-free "
        "PMW_RESEARCH_CONTRIBUTION_1 schema.\n\n"
        "At completion write exactly one canonical PMW_RUNTIME_BACKEND_OUTCOME_1 "
        f"JSON object (no trailing newline) to {result_path}. If the available tools "
        "cannot write that file, place the same canonical JSON in your final assistant "
        f"message between `{_ENVELOPE_BEGIN.strip()}` and "
        f"`{_ENVELOPE_END.strip()}` on their own lines. Do not invent durable refs.\n\n"
        "BEGIN_HOST_AUTHENTICATED_BRIEFING_JSON\n"
    ).encode("utf-8")
    middle = b"\nEND_HOST_AUTHENTICATED_BRIEFING_JSON\n\nBEGIN_HOST_INVOCATION_JSON\n"
    suffix = b"\nEND_HOST_INVOCATION_JSON\n"
    prompt_bytes = header + briefing + middle + invocation + suffix
    if not prompt_bytes or len(prompt_bytes) > config.maximum_prompt_bytes:
        _fail("PI_PROMPT_TOO_LARGE", str(len(prompt_bytes)))
    try:
        prompt = prompt_bytes.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise PiBackendError("PI_PROMPT_INPUT_INVALID") from error
    return prompt, {
        "protocol": PI_PROMPT_PROTOCOL,
        "prompt_bytes": len(prompt_bytes),
        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "briefing_bytes": len(briefing),
        "briefing_sha256": hashlib.sha256(briefing).hexdigest(),
        "invocation_bytes": len(invocation),
        "invocation_sha256": hashlib.sha256(invocation).hexdigest(),
    }


def _open_exclusive(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags, 0o600)
    except OSError as error:
        raise PiBackendError("PI_EVIDENCE_CREATE_FAILED", path.name) from error


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


class _BoundedEvidence:
    def __init__(
        self,
        *,
        path: Path,
        maximum_observed_bytes: int,
        maximum_retained_bytes: int,
        tail_bytes: int = 4_096,
    ) -> None:
        self.path = path
        self.maximum_observed_bytes = maximum_observed_bytes
        self.maximum_retained_bytes = maximum_retained_bytes
        self.tail_bytes = min(tail_bytes, maximum_retained_bytes)
        self.descriptor = _open_exclusive(path)
        self.observed_bytes = 0
        self.retained_bytes = 0
        self.digest = hashlib.sha256()
        self.tail = b""
        self.truncated = False
        self.cap_exceeded = False
        self.write_failed = False
        self.closed = False

    def append(self, raw: bytes) -> None:
        if self.closed:
            raise RuntimeError("evidence is closed")
        self.observed_bytes += len(raw)
        self.digest.update(raw)
        if raw:
            self.tail = (self.tail + raw)[-self.tail_bytes :]
        room = max(0, self.maximum_retained_bytes - self.retained_bytes)
        retained = raw[:room]
        if retained:
            try:
                _write_all(self.descriptor, retained)
                self.retained_bytes += len(retained)
            except OSError:
                self.write_failed = True
        if len(retained) != len(raw):
            self.truncated = True
        if self.observed_bytes > self.maximum_observed_bytes:
            self.cap_exceeded = True

    def close(self) -> None:
        if self.closed:
            return
        try:
            os.fsync(self.descriptor)
        except OSError:
            self.write_failed = True
        finally:
            try:
                os.close(self.descriptor)
            except OSError:
                self.write_failed = True
            self.closed = True

    def abort(self) -> None:
        if not self.closed:
            try:
                os.close(self.descriptor)
            except OSError:
                self.write_failed = True
            self.closed = True

    def to_value(self) -> dict[str, object]:
        return {
            "file": self.path.name,
            "observed_bytes": self.observed_bytes,
            "retained_bytes": self.retained_bytes,
            "observed_sha256": self.digest.hexdigest(),
            "tail_base64": base64.b64encode(self.tail).decode("ascii"),
            "truncated": self.truncated,
            "observed_safety_cap_exceeded": self.cap_exceeded,
            "write_failed": self.write_failed,
        }


def _rpc_json(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_number,
        )
        canonical_json(value)
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise PiRpcFailure("RPC_MALFORMED_JSON") from error
    if type(value) is not dict or type(value.get("type")) is not str:
        raise PiRpcFailure("RPC_MALFORMED_JSON")
    return value


class _PiRpcTransport:
    """One subprocess, one stdout reader, and exact request correlation."""

    def __init__(
        self,
        *,
        config: PiBackendConfig,
        request: SessionRequest,
        argv: tuple[str, ...],
        environment: Mapping[str, str],
        observer: PiRpcObserver | None = None,
        observer_evidence_keys: tuple[str, ...] = (),
    ) -> None:
        self.config = config
        self.request = request
        self.argv = argv
        self.environment = dict(environment)
        self.observer = observer
        self.observer_evidence_keys = observer_evidence_keys
        self.process: asyncio.subprocess.Process | None = None
        self.process_group_id: int | None = None
        self.frames = _BoundedEvidence(
            path=request.evidence / "pi.frames.jsonl",
            maximum_observed_bytes=config.maximum_stdout_bytes,
            maximum_retained_bytes=config.maximum_retained_frame_bytes,
        )
        try:
            self.stderr = _BoundedEvidence(
                path=request.evidence / "pi.stderr.bin",
                maximum_observed_bytes=config.maximum_stderr_bytes,
                maximum_retained_bytes=config.maximum_retained_stderr_bytes,
            )
        except Exception:
            self.frames.abort()
            try:
                self.frames.path.unlink()
            except OSError:
                pass
            raise
        self.event_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(
            maxsize=_EVENT_QUEUE_CAPACITY
        )
        self.pending: dict[
            str, tuple[str, asyncio.Future[dict[str, object]]]
        ] = {}
        self.failure: PiRpcFailure | None = None
        self.failure_event = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.observation_lock = asyncio.Lock()
        self.observation_count = 0
        self.observer_evidence: dict[str, object] = {}
        self.observer_finalize_failure: PiRpcFailure | None = None
        self.observer_finalized = observer is None
        self.request_count = 0
        self.frame_count = 0
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.shutdown_lock = asyncio.Lock()
        self.shutdown_task: asyncio.Task[StopProof] | None = None
        self.stop_proof: StopProof | None = None
        self.descendant_groups: tuple[int, ...] = ()
        self.descendant_discovery_failure: str | None = None
        self._evidence_closed = False

    def install_observer(self, observer: PiRpcObserver) -> None:
        if self.process is not None or self.observer is not None:
            raise PiBackendError("PI_OBSERVER_FACTORY_FAILED", "late observer")
        self.observer = observer
        self.observer_finalized = False

    async def _observe_frame(self, direction: str, raw: bytes) -> None:
        if self.observer is None:
            return
        async with self.observation_lock:
            await self._observe_frame_locked(direction, raw)

    async def _observe_frame_locked(self, direction: str, raw: bytes) -> None:
        observer = self.observer
        assert observer is not None
        if direction not in {
            PI_RPC_DIRECTION_HOST_TO_PI,
            PI_RPC_DIRECTION_PI_TO_HOST,
        }:
            raise AssertionError("invalid Pi RPC observation direction")
        self.observation_count += 1
        observation = PiRpcFrameObservation(
            direction=direction,
            raw_lf_json=bytes(raw),
            ordinal=self.observation_count,
            observed_at=datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            monotonic_ns=time.monotonic_ns(),
        )
        try:
            returned = await asyncio.wait_for(
                observer.observe(observation),
                timeout=float(self.config.response_timeout_seconds),
            )
        except asyncio.CancelledError as error:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise PiRpcFailure(
                "PI_OBSERVER_FAILED", "CancelledError"
            ) from error
        except Exception as error:  # noqa: BLE001
            raise PiRpcFailure(
                "PI_OBSERVER_FAILED", type(error).__name__
            ) from error
        if returned is not None:
            raise PiRpcFailure("PI_OBSERVER_FAILED", "non-null return")

    async def _write_host_frame(
        self,
        raw: bytes,
        *,
        timeout: float,
    ) -> None:
        assert self.process is not None and self.process.stdin is not None
        if self.observer is None:
            self.process.stdin.write(raw)
            await asyncio.wait_for(self.process.stdin.drain(), timeout=timeout)
            return
        # Keep the actual write/drain and its observation in the same lock.
        # A response may already be readable immediately after ``drain``;
        # the stdout reader must nevertheless receive the next ordinal.
        async with self.observation_lock:
            self.process.stdin.write(raw)
            await asyncio.wait_for(self.process.stdin.drain(), timeout=timeout)
            await self._observe_frame_locked(
                PI_RPC_DIRECTION_HOST_TO_PI,
                raw,
            )

    async def finalize_observer(self, outcome: BackendOutcome) -> None:
        proof = self.stop_proof
        if proof is None:
            raise AssertionError("observer finalization preceded transport stop proof")
        finality = PiRpcObserverFinality(
            backend_success=outcome.success,
            terminal_reason=outcome.terminal_reason,
            stop_proof=proof,
            observation_count=self.observation_count,
            transport_evidence=self.evidence_value(),
            backend_outcome=outcome,
        )
        await self._finalize_observer(finality=finality)

    async def _finalize_observer(
        self, *, finality: PiRpcObserverFinality
    ) -> None:
        if self.observer_finalized:
            return
        observer = self.observer
        assert observer is not None
        if not finality.stop_proof.stopped:
            self.observer_finalize_failure = PiRpcFailure(
                "PI_OBSERVER_FINALITY_UNPROVEN"
            )
            self.observer_finalized = True
            return
        try:
            returned = await asyncio.wait_for(
                observer.finalize(finality),
                timeout=float(self.config.response_timeout_seconds),
            )
            if returned is not None and not isinstance(returned, Mapping):
                raise PiRpcFailure(
                    "PI_OBSERVER_FINALIZE_FAILED", "evidence must be an object"
                )
            selected = {} if returned is None else dict(returned)
            keys = set(selected)
            if not keys.issubset(self.observer_evidence_keys):
                raise PiRpcFailure(
                    "PI_OBSERVER_FINALIZE_FAILED", "undeclared evidence key"
                )
            cloned = _bounded_json_clone(
                selected,
                maximum_bytes=MAXIMUM_PI_OBSERVER_EVIDENCE_BYTES,
                label="Pi observer evidence",
            )
            if type(cloned) is not dict:
                raise AssertionError("observer evidence clone is not an object")
            self.observer_evidence = cloned
        except asyncio.CancelledError as error:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            self.observer_finalize_failure = PiRpcFailure(
                "PI_OBSERVER_FINALIZE_FAILED", "CancelledError"
            )
        except PiRpcFailure as error:
            self.observer_finalize_failure = error
        except Exception as error:  # noqa: BLE001
            self.observer_finalize_failure = PiRpcFailure(
                "PI_OBSERVER_FINALIZE_FAILED", type(error).__name__
            )
        finally:
            self.observer_finalized = True

    async def spawn(self) -> None:
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.request.workspace,
                env=self.environment,
                limit=self.config.maximum_jsonl_line_bytes + 1,
                start_new_session=True,
            )
        except BaseException:
            self.frames.abort()
            self.stderr.abort()
            raise
        self.process_group_id = self.process.pid
        self.reader_task = asyncio.create_task(
            self._reader_loop(), name=f"{self.request.spec.session_id}:pi-stdout"
        )
        self.stderr_task = asyncio.create_task(
            self._stderr_loop(), name=f"{self.request.spec.session_id}:pi-stderr"
        )

    def _set_failure(self, failure: PiRpcFailure) -> None:
        if self.failure is not None:
            return
        self.failure = failure
        self.failure_event.set()
        for _command, future in self.pending.values():
            if not future.done():
                future.set_exception(failure)
        self.pending.clear()

    async def _reader_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                try:
                    raw = await self.process.stdout.readline()
                except ValueError as error:
                    raise PiRpcFailure("RPC_LINE_LIMIT") from error
                if not raw:
                    raise PiRpcFailure(
                        "RPC_EOF", f"returncode={self.process.returncode}"
                    )
                self.frames.append(raw)
                if (
                    not raw.endswith(b"\n")
                    or raw.endswith(b"\r\n")
                    or b"\r" in raw
                    or len(raw) > self.config.maximum_jsonl_line_bytes
                ):
                    raise PiRpcFailure("RPC_MALFORMED_FRAME")
                await self._observe_frame(
                    PI_RPC_DIRECTION_PI_TO_HOST,
                    raw,
                )
                if self.frames.write_failed:
                    raise PiRpcFailure("RPC_EVIDENCE_WRITE_FAILED")
                if self.frames.cap_exceeded:
                    raise PiRpcFailure("RPC_STDOUT_LIMIT")
                self.frame_count += 1
                if self.frame_count > self.config.maximum_frame_count:
                    raise PiRpcFailure("RPC_FRAME_LIMIT")
                frame = _rpc_json(raw[:-1])
                if frame["type"] == "response":
                    request_id = frame.get("id")
                    if type(request_id) is not str:
                        raise PiRpcFailure("RPC_RESPONSE_MISMATCH", "id")
                    pending = self.pending.pop(request_id, None)
                    if pending is None:
                        raise PiRpcFailure("RPC_RESPONSE_MISMATCH", "unknown id")
                    command, future = pending
                    if frame.get("command") != command:
                        raise PiRpcFailure("RPC_RESPONSE_MISMATCH", command)
                    if not future.done():
                        future.set_result(frame)
                else:
                    await self.event_queue.put(frame)
        except asyncio.CancelledError:
            raise
        except PiRpcFailure as error:
            self._set_failure(error)
        except Exception as error:  # noqa: BLE001
            self._set_failure(PiRpcFailure("RPC_READER_FAILED", type(error).__name__))

    async def _stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while True:
                chunk = await self.process.stderr.read(65_536)
                if not chunk:
                    return
                self.stderr.append(chunk)
                if self.stderr.write_failed:
                    raise PiRpcFailure("RPC_EVIDENCE_WRITE_FAILED", "stderr")
                if self.stderr.cap_exceeded:
                    raise PiRpcFailure("RPC_STDERR_LIMIT")
        except asyncio.CancelledError:
            raise
        except PiRpcFailure as error:
            self._set_failure(error)
        except Exception as error:  # noqa: BLE001
            self._set_failure(PiRpcFailure("RPC_STDERR_FAILED", type(error).__name__))

    async def call(
        self,
        command: str,
        body: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        if self.failure is not None:
            raise self.failure
        if self.process is None or self.process.stdin is None:
            raise PiRpcFailure("PROCESS_NOT_RUNNING")
        selected_timeout = (
            float(self.config.response_timeout_seconds)
            if timeout is None
            else timeout
        )
        async with self.send_lock:
            self.request_count += 1
            request_id = (
                f"{self.request.spec.session_id}:{self.request_count}:{command}"
            )
            request: dict[str, object] = {"id": request_id, "type": command}
            if body is not None:
                request.update(body)
            future: asyncio.Future[dict[str, object]] = (
                asyncio.get_running_loop().create_future()
            )
            self.pending[request_id] = (command, future)
            raw = canonical_json(request) + b"\n"
            try:
                await self._write_host_frame(
                    raw,
                    timeout=selected_timeout,
                )
            except PiRpcFailure as error:
                self.pending.pop(request_id, None)
                future.cancel()
                self._set_failure(error)
                raise
            except (TimeoutError, BrokenPipeError, ConnectionError) as error:
                self.pending.pop(request_id, None)
                future.cancel()
                raise PiRpcFailure("RPC_WRITE_FAILED", command) from error
        try:
            return await asyncio.wait_for(future, timeout=selected_timeout)
        except TimeoutError as error:
            self.pending.pop(request_id, None)
            raise PiRpcFailure("RPC_RESPONSE_TIMEOUT", command) from error

    async def next_event(self, *, timeout: float | None) -> dict[str, object]:
        event_task = asyncio.create_task(self.event_queue.get())
        failure_task = asyncio.create_task(self.failure_event.wait())
        try:
            done, pending = await asyncio.wait(
                {event_task, failure_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise PiRpcFailure("RPC_EVENT_TIMEOUT")
            if failure_task in done and self.failure is not None:
                raise self.failure
            return event_task.result()
        finally:
            for task in (event_task, failure_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(event_task, failure_task, return_exceptions=True)

    async def shutdown(
        self,
        *,
        reason: str,
        grace_seconds: float,
        abort_first: bool,
    ) -> StopProof:
        """Run exactly one cancellation-shielded terminal cleanup.

        The terminal proof is not published until process cleanup, pipe
        drainers, and evidence descriptors have all settled.  Later callers
        join the same task and cannot weaken or replace the first stop reason.
        """

        async with self.shutdown_lock:
            if self.shutdown_task is None:
                self.shutdown_task = asyncio.create_task(
                    self._shutdown_impl(
                        reason=reason,
                        grace_seconds=grace_seconds,
                        abort_first=abort_first,
                    ),
                    name=f"{self.request.spec.session_id}:pi-cleanup",
                )
            selected = self.shutdown_task
        return await asyncio.shield(selected)

    async def _shutdown_impl(
        self,
        *,
        reason: str,
        grace_seconds: float,
        abort_first: bool,
    ) -> StopProof:
        process = self.process
        group = self.process_group_id
        if process is None or group is None:
            proof = StopProof(
                stopped=True, reason=reason, detail="no Pi process was created"
            )
            self._close_evidence()
            self.stop_proof = proof
            return proof

        # Every phase is bounded.  The host deliberately joins this complete
        # terminal operation before it writes a receipt, so cleanup can never
        # become a detached background task.
        discovery_seconds = min(max(grace_seconds * 0.25, 0.05), 1.0)
        abort_seconds = min(
            max(grace_seconds * 0.25, 0.05),
            float(self.config.response_timeout_seconds),
        )
        termination_seconds = max(grace_seconds * 0.5, 0.05)
        base_proof: StopProof | None = None
        cleanup_error: str | None = None
        try:
            try:
                self.descendant_groups = await _discover_descendant_groups(
                    process.pid,
                    group,
                    timeout_seconds=discovery_seconds,
                )
            except PiRpcFailure as error:
                # Root-group cleanup may still protect the host, but detached
                # descendants are now unprovable.  Never turn this into a
                # successful stop proof.
                self.descendant_groups = ()
                self.descendant_discovery_failure = error.code
            if abort_first and process.returncode is None and self.failure is None:
                try:
                    response = await self.call(
                        "abort",
                        timeout=abort_seconds,
                    )
                    if response.get("success") is not True:
                        raise PiRpcFailure("RPC_COMMAND_REJECTED", "abort")
                except (PiRpcFailure, TimeoutError):
                    pass

            if process.stdin is not None:
                try:
                    process.stdin.close()
                    await asyncio.wait_for(
                        process.stdin.wait_closed(), timeout=abort_seconds
                    )
                except (TimeoutError, BrokenPipeError, ConnectionError):
                    pass
            try:
                base_proof = await _terminate_groups(
                    process,
                    group,
                    self.descendant_groups,
                    reason=reason,
                    grace_seconds=termination_seconds,
                )
            except (PiRpcFailure, OSError) as error:
                cleanup_error = getattr(error, "code", type(error).__name__)
                base_proof = StopProof(
                    stopped=False,
                    reason=reason,
                    process_group_id=group,
                    detail="cooperative process-group cleanup raised an error",
                )
        finally:
            for task in (self.reader_task, self.stderr_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (self.reader_task, self.stderr_task)
                    if task is not None
                ),
                return_exceptions=True,
            )
            self._close_evidence()
        assert base_proof is not None
        if self.descendant_discovery_failure is not None:
            proof = StopProof(
                stopped=False,
                reason=reason,
                forced=base_proof.forced,
                process_group_id=group,
                detail=(
                    "root group cleanup completed but detached descendant "
                    "absence is unproven"
                ),
            )
        elif cleanup_error is not None or not base_proof.stopped:
            proof = StopProof(
                stopped=False,
                reason=reason,
                forced=base_proof.forced,
                process_group_id=group,
                detail="cooperative process-group absence is unproven",
            )
        else:
            proof = base_proof
        # Publish only after the finally block has drained and closed all
        # transport evidence.  This is the idempotent terminal boundary.
        self.stop_proof = proof
        return proof

    def _close_evidence(self) -> None:
        if self._evidence_closed:
            return
        for selected in (self.frames, self.stderr):
            try:
                selected.close()
            except BaseException:
                selected.write_failed = True
                selected.abort()
        self._evidence_closed = True

    def evidence_value(self) -> dict[str, object]:
        value: dict[str, object] = {
            "protocol": PI_BACKEND_PROTOCOL,
            "single_stdout_reader": True,
            "strict_lf_jsonl": True,
            "process_group_id": self.process_group_id,
            "returncode": None if self.process is None else self.process.returncode,
            "request_count": self.request_count,
            "frame_count": self.frame_count,
            "frames": self.frames.to_value(),
            "stderr": self.stderr.to_value(),
            "descendant_process_groups": list(self.descendant_groups),
            "descendant_discovery_failure": self.descendant_discovery_failure,
            "stop_proof": (
                None if self.stop_proof is None else self.stop_proof.to_value()
            ),
        }
        if self.observer is not None:
            value["transport_observer"] = {
                "observation_count": self.observation_count,
                "finalized": self.observer_finalized,
                "finalize_failure": (
                    None
                    if self.observer_finalize_failure is None
                    else self.observer_finalize_failure.code
                ),
            }
        return value

    @property
    def evidence_write_failed(self) -> bool:
        return self.frames.write_failed or self.stderr.write_failed


def _group_alive(group: int) -> bool:
    if group == os.getpgrp():
        raise PiRpcFailure(
            "PROCESS_GROUP_IDENTITY_FAILURE", "refusing host process group"
        )
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _discover_descendant_groups(
    root_pid: int,
    root_group: int,
    *,
    timeout_seconds: float,
) -> tuple[int, ...]:
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            "/bin/ps",
            "-axo",
            "pid=,ppid=,pgid=",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except (OSError, TimeoutError) as error:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        raise PiRpcFailure("PROCESS_GROUP_DISCOVERY_FAILED") from error
    assert process is not None
    if process.returncode != 0 or len(stdout) > 16 * 1024 * 1024:
        raise PiRpcFailure("PROCESS_GROUP_DISCOVERY_FAILED")
    return _descendant_groups_from_ps(
        stdout,
        root_pid=root_pid,
        root_group=root_group,
    )


def _descendant_groups_from_ps(
    stdout: bytes,
    *,
    root_pid: int,
    root_group: int,
) -> tuple[int, ...]:
    """Parse one bounded POSIX ``ps`` snapshot.

    Linux exposes kernel processes whose PGID is zero in an all-process
    snapshot.  Those unrelated rows are valid input and must not turn every
    cooperative Pi cleanup into an unproven stop.  A zero/nonpositive group is
    still rejected if it is actually reachable from the managed root.
    """

    children: dict[int, list[tuple[int, int]]] = {}
    try:
        for raw in stdout.decode("ascii", errors="strict").splitlines():
            fields = raw.split()
            if len(fields) != 3:
                raise ValueError("malformed ps row")
            pid, parent, group = map(int, fields)
            if pid <= 0 or parent < 0 or group < 0:
                raise ValueError("invalid process identity")
            children.setdefault(parent, []).append((pid, group))
    except (UnicodeError, ValueError) as error:
        raise PiRpcFailure("PROCESS_GROUP_DISCOVERY_FAILED") from error
    pending = [root_pid]
    seen = {root_pid}
    groups: set[int] = set()
    while pending:
        parent = pending.pop()
        for pid, group in children.get(parent, ()):
            if pid in seen:
                continue
            seen.add(pid)
            pending.append(pid)
            if group <= 0:
                raise PiRpcFailure("PROCESS_GROUP_IDENTITY_FAILURE")
            if group != root_group:
                groups.add(group)
    if os.getpgrp() in groups:
        raise PiRpcFailure("PROCESS_GROUP_IDENTITY_FAILURE")
    return tuple(sorted(groups))


def _signal_groups(groups: tuple[int, ...], selected_signal: int) -> None:
    for group in groups:
        if group == os.getpgrp():
            raise PiRpcFailure("PROCESS_GROUP_IDENTITY_FAILURE")
        try:
            os.killpg(group, selected_signal)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            raise PiRpcFailure("PROCESS_GROUP_CLEANUP_FAILED") from error


async def _groups_absent(groups: tuple[int, ...], seconds: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, seconds)
    while True:
        if not any(_group_alive(group) for group in groups):
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(min(0.025, max(0.001, deadline - loop.time())))


async def _terminate_groups(
    process: asyncio.subprocess.Process,
    root_group: int,
    descendants: tuple[int, ...],
    *,
    reason: str,
    grace_seconds: float,
) -> StopProof:
    groups = tuple(sorted(set((*descendants, root_group))))
    alive = tuple(group for group in groups if _group_alive(group))
    forced = False
    detail = "cooperative process groups were already absent"
    if alive:
        detail = "SIGTERM sent to cooperative process groups"
        _signal_groups(alive, signal.SIGTERM)
        await _groups_absent(alive, max(0.0, grace_seconds))
    surviving = tuple(group for group in groups if _group_alive(group))
    if surviving:
        forced = True
        detail = "SIGKILL sent to cooperative process groups"
        _signal_groups(surviving, signal.SIGKILL)
        await _groups_absent(surviving, min(max(grace_seconds, 0.1), 5.0))
    if process.returncode is None:
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=min(max(grace_seconds, 0.1), 0.5),
            )
        except TimeoutError:
            pass
    stopped = not any(_group_alive(group) for group in groups)
    if not stopped:
        detail = "cooperative process groups could not be proven absent"
    return StopProof(
        stopped=stopped,
        reason=reason,
        forced=forced,
        process_group_id=root_group,
        detail=detail,
    )


def _response_data(response: Mapping[str, object], command: str) -> dict[str, object]:
    if response.get("success") is not True or type(response.get("data")) is not dict:
        raise PiRpcFailure("RPC_COMMAND_REJECTED", command)
    return response["data"]  # type: ignore[return-value]


def _validate_state(
    config: PiBackendConfig,
    state: Mapping[str, object],
    *,
    expected_session_id: str | None,
    require_idle: bool,
    expected_context_window_tokens: int | None,
) -> tuple[str, int]:
    model = state.get("model")
    if (
        type(model) is not dict
        or model.get("provider") != config.provider
        or model.get("id") != config.model
        or state.get("thinkingLevel") != config.thinking
        or type(model.get("contextWindow")) is not int
        or model["contextWindow"] <= 0
        or type(state.get("sessionId")) is not str
        or not state["sessionId"]
        or state.get("isCompacting") is not False
        or (require_idle and state.get("isStreaming") is not False)
        or (
            expected_context_window_tokens is not None
            and model.get("contextWindow") != expected_context_window_tokens
        )
    ):
        raise PiRpcFailure("RUNTIME_PROFILE_MISMATCH")
    session_id = state["sessionId"]
    if expected_session_id is not None and session_id != expected_session_id:
        raise PiRpcFailure("RUNTIME_SESSION_ID_DRIFT")
    return session_id, model["contextWindow"]  # type: ignore[return-value]


def _assistant_text(message: object) -> str | None:
    if type(message) is not dict or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if type(content) is str:
        return content
    if type(content) is not list:
        return None
    pieces: list[str] = []
    total = 0
    for block in content:
        if type(block) is dict and block.get("type") == "text":
            text = block.get("text")
            if type(text) is str:
                total += len(text.encode("utf-8", errors="strict"))
                if total > MAXIMUM_RESULT_BYTES * 2:
                    raise PiBackendError("PI_FINAL_MESSAGE_TOO_LARGE")
                pieces.append(text)
    return "".join(pieces)


def _provider_failure(message: object) -> PiRpcFailure | None:
    if type(message) is not dict or message.get("role") != "assistant":
        return None
    error = message.get("errorMessage")
    stop_reason = message.get("stopReason")
    if type(error) is not str or not error:
        if stop_reason == "error":
            return PiRpcFailure("PROVIDER_TERMINAL_ERROR", "stopReason=error")
        return None
    lowered = error.casefold()
    if any(
        marker in lowered
        for marker in (
            "usage_limit_reached",
            "usage limit reached",
            "insufficient_quota",
            "quota exceeded",
        )
    ):
        return PiRpcFailure("PROVIDER_QUOTA_EXHAUSTED")
    if any(
        marker in lowered
        for marker in (
            "context window",
            "maximum context length",
            "context_length_exceeded",
            "too many tokens",
        )
    ):
        return PiRpcFailure("PROVIDER_CONTEXT_LIMIT")
    return PiRpcFailure("PROVIDER_TERMINAL_ERROR")


def _bounded_json_clone(value: object, *, maximum_bytes: int, label: str) -> object:
    try:
        raw = canonical_json(value)
    except Exception as error:
        raise PiBackendError("PI_RUNTIME_METADATA_INVALID", label) from error
    if len(raw) > maximum_bytes:
        _fail("PI_RUNTIME_METADATA_INVALID", f"{label}: too large")
    return json.loads(raw.decode("utf-8"))


def _result_file(path: Path, maximum_bytes: int) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    return _read_regular_file(
        path, maximum_bytes=maximum_bytes, code="PI_RESULT_INVALID"
    )


def _result_envelope(text: str | None, maximum_bytes: int) -> bytes | None:
    if text is None:
        return None
    if text.count(_ENVELOPE_BEGIN) != 1 or text.count(_ENVELOPE_END) != 1:
        return None
    start = text.index(_ENVELOPE_BEGIN) + len(_ENVELOPE_BEGIN)
    end = text.index(_ENVELOPE_END, start)
    if end <= start:
        return None
    raw = text[start:end].encode("utf-8", errors="strict")
    if len(raw) > maximum_bytes:
        _fail("PI_RESULT_INVALID", "final envelope too large")
    return raw


def _parse_result(raw: bytes) -> BackendOutcome:
    value = _parse_json(raw, label="result")
    try:
        return BackendOutcome.from_value(value)
    except RuntimeContractError as error:
        raise PiBackendError("PI_RESULT_INVALID", "backend outcome") from error


class _PiUsageCollector:
    """Read Pi's reported token usage off the frames it actually emits.

    Pi reports a ``usage`` object on every completed message and on a
    completed compaction, and it answers ``get_session_stats`` with runtime
    totals.  This collector transcribes those numbers and nothing else: a
    field Pi omits stays omitted, and a session where Pi reported no usage at
    all yields ``UNMEASURED`` rather than a fabricated zero.
    """

    def __init__(self) -> None:
        self.records: list[UsageRequestRecord] = []
        self.observed_records = 0
        self.truncated = False
        self.session_totals: UsageTotals | None = None
        self.context_tokens: int | None = None
        self.context_window_tokens: int | None = None

    def observe_message(self, message: object) -> None:
        """Record one completed message when Pi reported its usage."""

        if type(message) is not dict:
            return
        # Metering must never be the thing that fails a session: a role Pi
        # spells oddly is recorded as unknown, not raised.
        role = _short_label(message.get("role"))
        self._append(
            usage=message.get("usage"),
            source_event="message_end",
            role="unknown" if role is None else role,
            provider=message.get("provider"),
            model=message.get("model"),
            stop_reason=message.get("stopReason"),
        )

    def observe_compaction(self, frame: Mapping[str, object]) -> None:
        """Record the summarization request a compaction paid for."""

        result = frame.get("result")
        if type(result) is not dict:
            return
        self._append(
            usage=result.get("usage"),
            source_event="compaction_end",
            role="compaction",
            provider=None,
            model=None,
            stop_reason=None,
        )

    def _append(
        self,
        *,
        usage: object,
        source_event: str,
        role: str,
        provider: object,
        model: object,
        stop_reason: object,
    ) -> None:
        if type(usage) is not dict:
            return
        counts = {
            "input_tokens": _observed_count(usage.get("input")),
            "cached_input_tokens": _observed_count(usage.get("cacheRead")),
            "cache_write_tokens": _observed_count(usage.get("cacheWrite")),
            "output_tokens": _observed_count(usage.get("output")),
            "reasoning_tokens": _observed_count(usage.get("reasoning")),
            "total_tokens": _observed_count(usage.get("totalTokens")),
        }
        if all(value is None for value in counts.values()):
            return
        self.observed_records += 1
        if len(self.records) >= MAXIMUM_USAGE_REQUEST_RECORDS:
            # Keep the aggregate honest by continuing to count the request
            # even once the per-request list is capped.
            self.truncated = True
            return
        self.records.append(
            UsageRequestRecord(
                ordinal=len(self.records) + 1,
                source_event=source_event,
                role=role,
                provider=_short_label(provider),
                model=_short_label(model),
                stop_reason=_short_label(stop_reason),
                **counts,
            )
        )

    def observe_session_stats(
        self,
        stats: Mapping[str, object],
        *,
        fallback_context_window_tokens: int | None = None,
    ) -> None:
        """Transcribe the runtime's own session-wide totals and context read."""

        tokens = stats.get("tokens")
        if type(tokens) is dict:
            totals = UsageTotals(
                basis=BASIS_RUNTIME_REPORTED_SESSION_TOTALS,
                request_count=None,
                input_tokens=_observed_count(tokens.get("input")),
                cached_input_tokens=_observed_count(tokens.get("cacheRead")),
                cache_write_tokens=_observed_count(tokens.get("cacheWrite")),
                output_tokens=_observed_count(tokens.get("output")),
                reasoning_tokens=None,
                total_tokens=_observed_count(tokens.get("total")),
            )
            if totals.reported_any_count:
                self.session_totals = totals
        context = stats.get("contextUsage")
        if type(context) is dict:
            self.context_tokens = _observed_count(context.get("tokens"))
            self.context_window_tokens = _observed_count(
                context.get("contextWindow")
            )
        if self.context_window_tokens is None:
            self.context_window_tokens = _observed_count(
                fallback_context_window_tokens
            )

    def evidence(self) -> UsageEvidence:
        """Return the block this session earned, measured or not."""

        totals: list[UsageTotals] = []
        if self.records:
            totals.append(_summed_totals(self.records))
        if self.session_totals is not None:
            totals.append(self.session_totals)
        if not totals:
            return UsageEvidence.unmeasured(
                provenance=PROVENANCE_PI_RPC_SURFACE_SILENT,
                detail=(
                    "Pi emitted no message, compaction, or session-stats "
                    "usage for this session"
                ),
            )
        return UsageEvidence.measured(
            provenance=PROVENANCE_PI_RPC_REPORTED,
            requests=tuple(self.records),
            requests_truncated=self.truncated,
            totals=tuple(totals),
            provider_reported_context_tokens=self.context_tokens,
            provider_reported_context_window_tokens=self.context_window_tokens,
            detail=(
                "transcribed from Pi RPC message_end/compaction_end frames "
                "and get_session_stats"
            ),
        )


def _short_label(value: object) -> str | None:
    if type(value) is not str or not value or len(value) > 512:
        return None
    return value if _MODEL.fullmatch(value) is not None else None


class _RunningPiSession:
    def __init__(
        self,
        *,
        config: PiBackendConfig,
        request: SessionRequest,
        transport: _PiRpcTransport,
        prompt: str,
        prompt_evidence: dict[str, object],
        result_path: Path,
    ) -> None:
        self.config = config
        self.request = request
        self.transport = transport
        self.prompt = prompt
        self.prompt_evidence = prompt_evidence
        self.result_path = result_path
        self.usage = _PiUsageCollector()
        self.stop_requested = asyncio.Event()
        self.stop_reason: str | None = None
        self.completion = asyncio.create_task(
            self._run_guarded(), name=f"{request.spec.session_id}:pi-session"
        )

    async def wait(self) -> BackendOutcome:
        return await asyncio.shield(self.completion)

    async def stop(self, reason: str, grace_seconds: float) -> StopProof:
        if type(reason) is not str or _REASON.fullmatch(reason) is None:
            raise RuntimeContractError("MALFORMED_STOP_PROOF", "reason")
        if (
            type(grace_seconds) not in {int, float}
            or not 0 <= grace_seconds <= MAXIMUM_STOP_GRACE_SECONDS
        ):
            raise ValueError("grace_seconds is out of bounds")
        if self.stop_reason is None:
            self.stop_reason = reason
            self.stop_requested.set()
        proof = await self.transport.shutdown(
            reason=self.stop_reason,
            grace_seconds=float(grace_seconds),
            abort_first=True,
        )
        # A positive process proof is not the whole adapter boundary.  Join
        # the session coroutine as well so no blocked event waiter survives a
        # terminal receipt.  ``_run_guarded`` joins the same idempotent
        # transport shutdown and returns a bounded failure outcome.
        if asyncio.current_task() is not self.completion:
            await asyncio.shield(self.completion)
        return proof

    async def _next_event_or_stop(self) -> dict[str, object]:
        event_task = asyncio.create_task(
            self.transport.next_event(timeout=None)
        )
        stop_task = asyncio.create_task(self.stop_requested.wait())
        try:
            done, _pending = await asyncio.wait(
                {event_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done and self.stop_requested.is_set():
                raise PiRpcFailure("STOP_REQUESTED")
            return event_task.result()
        finally:
            for task in (event_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                event_task, stop_task, return_exceptions=True
            )

    async def _run_guarded(self) -> BackendOutcome:
        try:
            outcome = await self._run()
        except asyncio.CancelledError:
            await asyncio.shield(
                self.transport.shutdown(
                    reason="RUNTIME_CANCELLED",
                    grace_seconds=float(self.request.stop_grace_seconds),
                    abort_first=True,
                )
            )
            raise
        except Exception as error:  # noqa: BLE001
            proof = await self.transport.shutdown(
                reason="BACKEND_RUNTIME_FAILURE",
                grace_seconds=float(self.request.stop_grace_seconds),
                abort_first=True,
            )
            code = getattr(error, "code", "BACKEND_RUNTIME_FAILURE")
            if not proof.stopped:
                code = "PROCESS_GROUP_CLEANUP_FAILED"
            elif self.transport.evidence_write_failed:
                code = "RPC_EVIDENCE_WRITE_FAILED"
            outcome = self._failure(
                code,
                f"Pi RPC adapter failed: {type(error).__name__}",
                proof,
            )
        await self.transport.finalize_observer(outcome)
        return self._merge_observer_evidence(outcome)

    async def _run(self) -> BackendOutcome:
        state_before = _response_data(
            await self.transport.call("get_state"), "get_state"
        )
        pi_session_id, context_window = _validate_state(
            self.config,
            state_before,
            expected_session_id=None,
            require_idle=True,
            expected_context_window_tokens=self.request.context_window_tokens,
        )
        response = await self.transport.call("prompt", {"message": self.prompt})
        if response.get("success") is not True:
            raise PiRpcFailure("PROMPT_REJECTED")

        final_message: object = None
        final_text: str | None = None
        assistant_usage: object = None
        retry_events = 0
        compaction_events = 0
        while True:
            if self.stop_requested.is_set():
                raise PiRpcFailure("STOP_REQUESTED")
            # A provider may legitimately remain silent for much longer than
            # a zero-model RPC response.  The host wall timer is the only
            # research-generation timeout, while stop remains immediately
            # observable even when no provider event arrives.
            frame = await self._next_event_or_stop()
            event_type = frame.get("type")
            if event_type in {"auto_retry_start", "auto_retry_end"}:
                retry_events += 1
            elif event_type in {"compaction_start", "compaction_end"}:
                compaction_events += 1
                if event_type == "compaction_end":
                    self.usage.observe_compaction(frame)
            elif event_type == "extension_error":
                raise PiRpcFailure("PI_EXTENSION_ERROR")
            elif event_type == "message_end":
                message = frame.get("message")
                # Every completed message is metered, including the tool-call
                # turns that carry no final text.  Metering the last text
                # message alone would hide most of a session's real cost.
                self.usage.observe_message(message)
                failure = _provider_failure(message)
                if failure is not None:
                    raise failure
                text = _assistant_text(message)
                if text is not None:
                    final_message = message
                    final_text = text
                    if type(message) is dict:
                        assistant_usage = message.get("usage")
            elif event_type == "agent_settled":
                break
        if final_message is None:
            raise PiRpcFailure("ASSISTANT_RESULT_MISSING")

        state_after = _response_data(
            await self.transport.call("get_state"), "get_state"
        )
        _session_after, context_after = _validate_state(
            self.config,
            state_after,
            expected_session_id=pi_session_id,
            require_idle=True,
            expected_context_window_tokens=self.request.context_window_tokens,
        )
        if context_after != context_window:
            raise PiRpcFailure("RUNTIME_CONTEXT_REPORT_DRIFT")
        session_stats = _response_data(
            await self.transport.call("get_session_stats"), "get_session_stats"
        )
        self.usage.observe_session_stats(
            session_stats,
            fallback_context_window_tokens=context_window,
        )

        raw_result = _result_file(
            self.result_path, self.config.maximum_result_bytes
        )
        result_source = "workspace_file"
        if raw_result is None:
            raw_result = _result_envelope(
                final_text, self.config.maximum_result_bytes
            )
            result_source = "final_message_envelope"
        if raw_result is None:
            raise PiBackendError("PI_RESULT_MISSING")
        reported = _parse_result(raw_result)
        if "pi_rpc" in reported.evidence:
            raise PiBackendError("PI_RESULT_INVALID", "reserved evidence key")
        if set(reported.evidence).intersection(
            self.transport.observer_evidence_keys
        ):
            raise PiBackendError(
                "PI_RESULT_INVALID", "reserved observer evidence key"
            )
        if "pi_rpc" in reported.usage:
            raise PiBackendError("PI_RESULT_INVALID", "reserved usage key")

        proof = await self.transport.shutdown(
            reason="PROCESS_EXIT",
            grace_seconds=float(self.request.stop_grace_seconds),
            abort_first=False,
        )
        if not proof.stopped:
            return self._failure(
                "PROCESS_GROUP_CLEANUP_FAILED",
                "Pi process group could not be proven stopped",
                proof,
            )
        if self.transport.evidence_write_failed:
            return self._failure(
                "RPC_EVIDENCE_WRITE_FAILED",
                "Pi transport evidence could not be durably closed",
                proof,
            )
        runtime = self._runtime_evidence(proof)
        runtime["prompt"] = dict(self.prompt_evidence)
        runtime["pi_session_id"] = pi_session_id
        runtime["pi_reported_context_window"] = context_window
        runtime["configured_context_window_tokens"] = (
            self.request.context_window_tokens
        )
        runtime["account_route_context_acceptance"] = "NOT_MEASURED_BY_ADAPTER"
        runtime["context_window_control"] = (
            ContextWindowControl.NATIVE_MODEL_WINDOW.value
            if self.request.context_window_tokens is not None
            else "BACKEND_DECLARED_MODEL_WINDOW"
        )
        runtime["strict_pre_http_input_gate"] = False
        runtime["host_prompt_count"] = 1
        runtime["host_retry_count"] = 0
        runtime["host_compaction_count"] = 0
        runtime["pi_retry_compaction_policy"] = (
            "PINNED_PI_CONFIG_NOT_HOST_OVERRIDDEN"
        )
        runtime["observed_pi_retry_events"] = retry_events
        runtime["observed_pi_compaction_events"] = compaction_events
        runtime["observed_pi_usage_reports"] = self.usage.observed_records
        runtime["result_source"] = result_source
        runtime["result_sha256"] = hashlib.sha256(raw_result).hexdigest()

        evidence = reported.evidence
        evidence["pi_rpc"] = runtime
        usage = reported.usage
        usage["pi_rpc"] = {
            "provider": self.config.provider,
            "model": self.config.model,
            "thinking": self.config.thinking,
            "pi_reported_context_window": context_window,
            "configured_context_window_tokens": (
                self.request.context_window_tokens
            ),
            "account_route_context_acceptance": "NOT_MEASURED_BY_ADAPTER",
            "assistant_usage": _bounded_json_clone(
                assistant_usage,
                maximum_bytes=131_072,
                label="assistant usage",
            ),
            "session_stats": _bounded_json_clone(
                session_stats,
                maximum_bytes=262_144,
                label="session stats",
            ),
        }
        return BackendOutcome(
            success=reported.success,
            terminal_reason=reported.terminal_reason,
            summary=reported.summary,
            contributions=reported.contributions,
            usage=usage,
            evidence=evidence,
            usage_evidence=self.usage.evidence(),
        )

    def _merge_observer_evidence(
        self,
        outcome: BackendOutcome,
    ) -> BackendOutcome:
        if self.transport.observer is None:
            return outcome
        proof = self.transport.stop_proof
        if proof is None:
            raise AssertionError("observer settlement preceded transport shutdown")
        failure = self.transport.observer_finalize_failure
        if failure is not None:
            reason = (
                "PROCESS_GROUP_CLEANUP_FAILED"
                if failure.code == "PI_OBSERVER_FINALITY_UNPROVEN"
                else "PI_OBSERVER_FINALIZE_FAILED"
            )
            return self._failure(
                reason,
                "Pi RPC transport observer could not be finalized",
                proof,
            )
        evidence = outcome.evidence
        if set(evidence).intersection(self.transport.observer_evidence):
            return self._failure(
                "PI_OBSERVER_FINALIZE_FAILED",
                "Pi RPC transport observer evidence key conflict",
                proof,
            )
        evidence.update(self.transport.observer_evidence)
        try:
            return BackendOutcome(
                success=outcome.success,
                terminal_reason=outcome.terminal_reason,
                summary=outcome.summary,
                contributions=outcome.contributions,
                usage=outcome.usage,
                evidence=evidence,
                usage_evidence=outcome.usage_evidence,
            )
        except RuntimeContractError:
            return self._failure(
                "PI_OBSERVER_FINALIZE_FAILED",
                "Pi RPC transport observer evidence was not admissible",
                proof,
            )

    def _runtime_evidence(self, proof: StopProof) -> dict[str, object]:
        value = self.transport.evidence_value()
        value["stop_proof"] = proof.to_value()
        return value

    def _failure(
        self,
        reason: str,
        summary: str,
        proof: StopProof,
    ) -> BackendOutcome:
        # A session that failed part-way still spent whatever tokens Pi had
        # already reported.  Carry that reading into the failure receipt
        # instead of losing the cost of the attempt.
        return BackendOutcome(
            success=False,
            terminal_reason=(
                reason if _REASON.fullmatch(reason) is not None else "PI_RUNTIME_FAILED"
            ),
            summary=summary,
            evidence={"pi_rpc": self._runtime_evidence(proof)},
            usage_evidence=self.usage.evidence(),
        )


async def _join_task_despite_cancellation(
    task: asyncio.Task[object],
) -> asyncio.CancelledError | None:
    """Join a shielded pre-handoff task and remember caller cancellation."""

    observed: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            observed = observed or error
        except BaseException:
            break
    # Retrieve any exception so a joined verifier/spawn task cannot produce an
    # unhandled-task warning.  Its semantic error is secondary once start was
    # cancelled; process cleanup below remains authoritative.
    if task.done():
        try:
            task.result()
        except BaseException:
            pass
    return observed


async def _shutdown_despite_cancellation(
    transport: _PiRpcTransport,
    *,
    reason: str,
    grace_seconds: float,
    abort_first: bool,
) -> tuple[StopProof, asyncio.CancelledError | None]:
    """Join terminal cleanup even if the enclosing start is cancelled again."""

    cleanup = asyncio.create_task(
        transport.shutdown(
            reason=reason,
            grace_seconds=grace_seconds,
            abort_first=abort_first,
        )
    )
    observed: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(cleanup), observed
        except asyncio.CancelledError as error:
            if cleanup.cancelled():
                return (
                    StopProof(
                        stopped=False,
                        reason=reason,
                        process_group_id=transport.process_group_id,
                        detail="terminal cleanup task was cancelled",
                    ),
                    observed or error,
                )
            observed = observed or error
        except BaseException:
            return (
                StopProof(
                    stopped=False,
                    reason=reason,
                    process_group_id=transport.process_group_id,
                    detail="terminal cleanup task raised an error",
                ),
                observed,
            )


class PiBackend:
    """Run one generic research prompt through Pi's account-OAuth RPC mode."""

    def __init__(
        self,
        config: PiBackendConfig,
        *,
        observer_factory: PiRpcObserverFactory | None = None,
    ) -> None:
        if not isinstance(config, PiBackendConfig):
            raise TypeError("config must be PiBackendConfig")
        self._config = config
        self._observer_factory = observer_factory
        self._observer_identity: BackendIdentity | None = None
        self._observer_evidence_keys: tuple[str, ...] = ()
        public_config = config.to_public_value()
        if observer_factory is not None:
            observer_identity, evidence_keys = _observer_factory_contract(
                observer_factory
            )
            self._observer_identity = observer_identity
            self._observer_evidence_keys = evidence_keys
            public_config["transport_observer"] = {
                "factory_identity": observer_identity.to_value(),
                "evidence_keys": list(evidence_keys),
                "frame_contract": {
                    "directions": [
                        PI_RPC_DIRECTION_HOST_TO_PI,
                        PI_RPC_DIRECTION_PI_TO_HOST,
                    ],
                    "bytes": "EXACT_LF_JSON",
                    "ordering": "HOST_TOTAL_ORDINAL_SINGLE_LOCK",
                    "clocks": ["UTC_OBSERVED_AT", "HOST_MONOTONIC_NS"],
                    "observer_can_replace_frame": False,
                    "observer_failure": "SESSION_FAIL_CLOSED",
                },
            }
        self._identity = BackendIdentity(
            name=config.name,
            protocol=PI_BACKEND_PROTOCOL,
            public_config=public_config,
        )
        self._verification_lock = asyncio.Lock()

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    @property
    def context_window_control(self) -> ContextWindowControl:
        return ContextWindowControl.NATIVE_MODEL_WINDOW

    def verify_runtime(self) -> None:
        """Recheck every pinned runtime input without starting Pi."""

        self._config.verify_runtime()
        if self._observer_factory is not None:
            identity, evidence_keys = _observer_factory_contract(
                self._observer_factory
            )
            if (
                identity != self._observer_identity
                or evidence_keys != self._observer_evidence_keys
            ):
                _fail("PI_OBSERVER_FACTORY_DRIFT")

    def _create_observer(self, request: SessionRequest) -> PiRpcObserver | None:
        factory = self._observer_factory
        if factory is None:
            return None
        try:
            observer = factory.create(request)
        except Exception as error:  # noqa: BLE001
            raise PiBackendError(
                "PI_OBSERVER_FACTORY_FAILED", type(error).__name__
            ) from error
        if (
            observer is None
            or not callable(getattr(observer, "observe", None))
            or not callable(getattr(observer, "finalize", None))
        ):
            raise PiBackendError("PI_OBSERVER_FACTORY_FAILED", "observer")
        return observer

    def validate_context_window_policy(
        self,
        policy: ContextWindowPolicy,
        session_ids: tuple[str, ...],
    ) -> None:
        del session_ids
        if not isinstance(policy, ContextWindowPolicy):
            raise TypeError("policy must be ContextWindowPolicy")
        if policy.configured and self._config.extensions:
            _fail(
                "PI_CONTEXT_EXTENSION_COMPATIBILITY_UNPROVEN",
                "configured context windows require no external Pi extensions",
            )

    async def _verify_runtime(self) -> None:
        async with self._verification_lock:
            await asyncio.to_thread(self.verify_runtime)

    async def start(self, request: SessionRequest) -> _RunningPiSession:
        if not isinstance(request, SessionRequest):
            raise TypeError("request must be SessionRequest")
        transport: _PiRpcTransport | None = None
        spawn_task: asyncio.Task[None] | None = None
        verification_task: asyncio.Task[None] | None = None
        handed_off = False
        try:
            if request.context_window_tokens is not None and self._config.extensions:
                _fail("PI_CONTEXT_EXTENSION_COMPATIBILITY_UNPROVEN")
            verification_task = asyncio.create_task(self._verify_runtime())
            await asyncio.shield(verification_task)
            verification_task = None
            _require_session_layout(request)
            session_dir = _mkdir_private(request.private_root, "pi-sessions")
            result_path = request.workspace / self._config.result_path
            if result_path.exists() or result_path.is_symlink():
                _fail("PI_RESULT_INVALID", "result path already exists")
            prompt, prompt_evidence = _build_prompt(
                self._config, request, result_path
            )
            environment = _session_environment(
                self._config, request, session_dir=session_dir
            )
            argv = _build_argv(self._config, request, session_dir)
            transport = _PiRpcTransport(
                config=self._config,
                request=request,
                argv=argv,
                environment=environment,
                observer_evidence_keys=self._observer_evidence_keys,
            )
            observer = self._create_observer(request)
            if observer is not None:
                transport.install_observer(observer)
            spawn_task = asyncio.create_task(transport.spawn())
            await asyncio.shield(spawn_task)
            spawn_task = None
            verification_task = asyncio.create_task(self._verify_runtime())
            await asyncio.shield(verification_task)
            verification_task = None
            running = _RunningPiSession(
                config=self._config,
                request=request,
                transport=transport,
                prompt=prompt,
                prompt_evidence=prompt_evidence,
                result_path=result_path,
            )
            handed_off = True
            return running
        except asyncio.CancelledError as cancelled:
            # Cancellation may arrive while the shielded spawn or the
            # post-spawn pin verifier is still running.  Join either task
            # before classifying the start, then prove every created process
            # gone.  No cancellation edge may bypass the handoff boundary.
            for task in (spawn_task, verification_task):
                if task is not None:
                    await _join_task_despite_cancellation(task)  # type: ignore[arg-type]
            if transport is not None and transport.process is not None:
                proof, _repeated_cancel = await _shutdown_despite_cancellation(
                    transport,
                    reason="START_CANCELLED",
                    grace_seconds=float(request.stop_grace_seconds),
                    abort_first=False,
                )
            else:
                if transport is not None:
                    proof, _repeated_cancel = await _shutdown_despite_cancellation(
                        transport,
                        reason="START_CANCELLED",
                        grace_seconds=float(request.stop_grace_seconds),
                        abort_first=False,
                    )
                else:
                    proof = StopProof(
                        stopped=True,
                        reason="START_CANCELLED",
                        detail="no Pi process was created",
                    )
            raise BackendStartError(
                "PI_START_CANCELLED", stop_proof=proof
            ) from cancelled
        except BackendStartError:
            raise
        except Exception as error:
            if transport is not None and transport.process is not None and not handed_off:
                proof, cleanup_cancel = await _shutdown_despite_cancellation(
                    transport,
                    reason="START_FAILED",
                    grace_seconds=float(request.stop_grace_seconds),
                    abort_first=False,
                )
                if cleanup_cancel is not None:
                    raise BackendStartError(
                        "PI_START_CANCELLED", stop_proof=proof
                    ) from cleanup_cancel
            else:
                if transport is not None:
                    proof = await transport.shutdown(
                        reason="START_FAILED",
                        grace_seconds=float(request.stop_grace_seconds),
                        abort_first=False,
                    )
                else:
                    proof = StopProof(
                        stopped=True,
                        reason="START_FAILED",
                        detail="no Pi process was created",
                    )
            raise BackendStartError(
                "PI_START_FAILED",
                type(error).__name__,
                stop_proof=proof,
            ) from error


def load_pi_backend(
    path: Path,
    *,
    observer_factory: PiRpcObserverFactory | None = None,
) -> PiBackend:
    """Construct a Pi backend from one strict JSON configuration."""

    return PiBackend(
        load_pi_backend_config(path),
        observer_factory=observer_factory,
    )


__all__ = [
    "PI_BACKEND_CONFIG_SCHEMA",
    "PI_BACKEND_PROTOCOL",
    "PI_PROMPT_PROTOCOL",
    "PI_RPC_DIRECTION_HOST_TO_PI",
    "PI_RPC_DIRECTION_PI_TO_HOST",
    "PiBackend",
    "PiBackendConfig",
    "PiBackendError",
    "PiRpcFailure",
    "PiRpcFrameObservation",
    "PiRpcObserverFinality",
    "PiRpcObserver",
    "PiRpcObserverFactory",
    "load_pi_backend",
    "load_pi_backend_config",
]
