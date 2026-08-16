"""A local, model-free command backend for the generic session runtime.

This adapter deliberately has a small trust surface.  Its configuration is a
strict bounded JSON document, its command line is public launch identity, and it
does not inherit the host environment.  The child receives only session-local
paths and fixed locale/PATH values.  In particular, OAuth credentials, API
keys, proxy credentials, and arbitrary caller environment variables have no
inheritance path.

``start_new_session`` and process-group cleanup provide *cooperative process
group containment*.  They are useful lifecycle controls, not an OS sandbox:
an adversarial command can still access resources allowed to the host process
or deliberately escape its process group.  A stronger backend may place this
same protocol behind Seatbelt, namespaces, a VM, or another real sandbox.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePath
import re
import signal
import stat
from typing import NoReturn

from .contracts import (
    BackendIdentity,
    BackendOutcome,
    BackendStartError,
    MAXIMUM_STOP_GRACE_SECONDS,
    RuntimeContractError,
    SessionRequest,
    StopProof,
)
from .safety import BoundedCaptureAccumulator, CaptureLimits, CaptureSnapshot
from ..world.records import canonical_json


COMMAND_BACKEND_CONFIG_SCHEMA = "PMW_COMMAND_BACKEND_CONFIG_1"
COMMAND_BACKEND_PROTOCOL = "PMW_LOCAL_COMMAND_1"
MAXIMUM_CONFIG_BYTES = 65_536
MAXIMUM_RESULT_BYTES = 8 * 1024 * 1024
MAXIMUM_CAPTURE_RETAINED_BYTES = 256 * 1024 * 1024
MAXIMUM_CAPTURE_TAIL_BYTES = 256 * 1024
MAXIMUM_EXECUTABLE_BYTES = 512 * 1024 * 1024
MAXIMUM_ARGV_BYTES = 256 * 1024
MAXIMUM_ARGUMENT_BYTES = 65_536
_MAXIMUM_JSON_INTEGER = (1 << 63) - 1
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_RESULT_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONFIG_FIELDS = {
    "schema",
    "name",
    "argv",
    "argv_is_public",
    "result_path",
    "capture",
}
_CAPTURE_FIELDS = {
    "maximum_retained_bytes",
    "maximum_observed_bytes",
    "tail_bytes",
}


class CommandBackendError(ValueError):
    """A stable configuration, result, or local-runtime validation error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _fail(code: str, detail: str = "") -> NoReturn:
    raise CommandBackendError(code, detail)


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


def _strict_json(
    raw: bytes,
    *,
    maximum_bytes: int,
    label: str,
) -> object:
    if type(raw) is not bytes or not raw or len(raw) > maximum_bytes:
        _fail("COMMAND_JSON_SIZE_INVALID", label)
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_int=_bounded_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise CommandBackendError("MALFORMED_COMMAND_JSON", label) from error
    try:
        canonical_json(value)
    except Exception as error:
        raise CommandBackendError("MALFORMED_COMMAND_JSON", label) from error
    return value


def _open_readonly_nofollow(path: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
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
        file_descriptor = _open_readonly_nofollow(path)
    except OSError as error:
        raise CommandBackendError(code, "open failed") from error
    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(code, "not a regular file")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(file_descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum_bytes:
            _fail(code, "file is too large")
        return raw
    finally:
        os.close(file_descriptor)


def _require_positive_integer(
    value: object,
    *,
    label: str,
    maximum: int | None = None,
) -> int:
    if type(value) is not int or value <= 0:
        _fail("MALFORMED_COMMAND_CONFIG", label)
    if maximum is not None and value > maximum:
        _fail("MALFORMED_COMMAND_CONFIG", label)
    return value


def _validate_result_path(value: object) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 1_024:
        _fail("MALFORMED_COMMAND_CONFIG", "result_path")
    selected = PurePath(value)
    if selected.is_absolute() or len(selected.parts) != 1:
        _fail("MALFORMED_COMMAND_CONFIG", "result_path")
    if any(
        component in {"", ".", ".."}
        or _RESULT_COMPONENT.fullmatch(component) is None
        for component in selected.parts
    ):
        _fail("MALFORMED_COMMAND_CONFIG", "result_path")
    return value


def _hash_executable(path: Path) -> tuple[str, tuple[int, int, int, int]]:
    if not path.is_absolute():
        _fail("COMMAND_EXECUTABLE_INVALID", "argv[0] must be absolute")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CommandBackendError(
            "COMMAND_EXECUTABLE_INVALID", "lstat failed"
        ) from error
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        _fail("COMMAND_EXECUTABLE_INVALID", "not an exact regular file")
    if metadata.st_mode & 0o111 == 0:
        _fail("COMMAND_EXECUTABLE_INVALID", "not executable")

    digest = hashlib.sha256()
    try:
        file_descriptor = _open_readonly_nofollow(path)
    except OSError as error:
        raise CommandBackendError(
            "COMMAND_EXECUTABLE_INVALID", "open failed"
        ) from error
    try:
        opened = os.fstat(file_descriptor)
        if not stat.S_ISREG(opened.st_mode):
            _fail("COMMAND_EXECUTABLE_INVALID", "not a regular file")
        if not 0 < opened.st_size <= MAXIMUM_EXECUTABLE_BYTES:
            _fail("COMMAND_EXECUTABLE_INVALID", "file is too large or empty")
        copied = 0
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > MAXIMUM_EXECUTABLE_BYTES:
                _fail("COMMAND_EXECUTABLE_DRIFT", "file grew beyond size limit")
            digest.update(chunk)
        after = os.fstat(file_descriptor)
    finally:
        os.close(file_descriptor)
    identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    expected = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )
    opened_identity = (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    )
    if (
        identity != expected
        or identity != opened_identity
        or copied != opened.st_size
    ):
        _fail("COMMAND_EXECUTABLE_DRIFT", "changed while hashing")
    return digest.hexdigest(), identity


async def _materialize_executable(
    config: "CommandBackendConfig",
    private_root: Path,
) -> Path:
    """Copy the verified inode once, then execute only that private copy.

    Hashing a pathname and later passing the same pathname to ``exec`` leaves
    a rename race.  Reading through one no-follow descriptor and hashing the
    bytes written to an exclusive session-local file closes that gap for this
    cooperative backend.
    """

    source = Path(config.argv[0])
    destination = private_root / "command-executable"
    source_fd: int | None = None
    destination_fd: int | None = None
    destination_created = False
    complete = False
    try:
        source_fd = _open_readonly_nofollow(source)
        before = os.fstat(source_fd)
        observed_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        if observed_identity != config._executable_identity:
            _fail("COMMAND_EXECUTABLE_DRIFT")
        if not 0 < before.st_size <= MAXIMUM_EXECUTABLE_BYTES:
            _fail("COMMAND_EXECUTABLE_INVALID", "file is too large or empty")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        destination_fd = os.open(destination, flags, 0o500)
        destination_created = True
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            if copied > MAXIMUM_EXECUTABLE_BYTES:
                _fail("COMMAND_EXECUTABLE_DRIFT", "file grew beyond size limit")
            _write_all(destination_fd, chunk)
            # Keep the event loop responsive without moving this security-
            # sensitive single-FD copy into an uncancellable worker thread.
            await asyncio.sleep(0)
        after = os.fstat(source_fd)
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if (
            after_identity != observed_identity
            or copied != before.st_size
            or digest.hexdigest() != config.executable_sha256
        ):
            _fail("COMMAND_EXECUTABLE_DRIFT")
        os.fchmod(destination_fd, 0o500)
        os.fsync(destination_fd)
        complete = True
    except CommandBackendError:
        raise
    except OSError as error:
        raise CommandBackendError("COMMAND_EXECUTABLE_MATERIALIZE_FAILED") from error
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
        if destination_created and not complete:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # The containing session remains unusable and start fails;
                # never execute an incomplete private copy.
                pass
    return destination


@dataclass(frozen=True, slots=True)
class CommandBackendConfig:
    """Validated public command configuration.

    There is intentionally no arbitrary environment mapping.  ``argv`` is
    declared public and is included verbatim in :class:`BackendIdentity`; it
    must therefore never carry a password, bearer token, or similar secret.
    """

    name: str
    argv: tuple[str, ...]
    result_path: str
    capture_limits: CaptureLimits
    executable_sha256: str
    _executable_identity: tuple[int, int, int, int] = field(repr=False)

    @classmethod
    def from_value(cls, value: object) -> "CommandBackendConfig":
        if type(value) is not dict or set(value) != _CONFIG_FIELDS:
            _fail("MALFORMED_COMMAND_CONFIG", "fields")
        if value.get("schema") != COMMAND_BACKEND_CONFIG_SCHEMA:
            _fail("MALFORMED_COMMAND_CONFIG", "schema")
        name = value.get("name")
        if type(name) is not str or _NAME.fullmatch(name) is None:
            _fail("MALFORMED_COMMAND_CONFIG", "name")
        if value.get("argv_is_public") is not True:
            _fail("MALFORMED_COMMAND_CONFIG", "argv_is_public")
        raw_argv = value.get("argv")
        if type(raw_argv) is not list or not raw_argv or len(raw_argv) > 4_096:
            _fail("MALFORMED_COMMAND_CONFIG", "argv")
        argv: list[str] = []
        total_argv_bytes = 0
        for index, argument in enumerate(raw_argv):
            if type(argument) is not str or "\x00" in argument:
                _fail("MALFORMED_COMMAND_CONFIG", f"argv[{index}]")
            encoded = argument.encode("utf-8", errors="strict")
            if not encoded or len(encoded) > MAXIMUM_ARGUMENT_BYTES:
                _fail("MALFORMED_COMMAND_CONFIG", f"argv[{index}]")
            total_argv_bytes += len(encoded) + 1
            argv.append(argument)
        if total_argv_bytes > MAXIMUM_ARGV_BYTES:
            _fail("MALFORMED_COMMAND_CONFIG", "argv")

        executable_sha256, executable_identity = _hash_executable(Path(argv[0]))
        result_path = _validate_result_path(value.get("result_path"))
        raw_capture = value.get("capture")
        if type(raw_capture) is not dict or set(raw_capture) != _CAPTURE_FIELDS:
            _fail("MALFORMED_COMMAND_CONFIG", "capture")
        try:
            capture_limits = CaptureLimits(
                maximum_retained_bytes=_require_positive_integer(
                    raw_capture.get("maximum_retained_bytes"),
                    label="capture.maximum_retained_bytes",
                    maximum=MAXIMUM_CAPTURE_RETAINED_BYTES,
                ),
                maximum_observed_bytes=_require_positive_integer(
                    raw_capture.get("maximum_observed_bytes"),
                    label="capture.maximum_observed_bytes",
                    maximum=1 << 40,
                ),
                tail_bytes=_require_positive_integer(
                    raw_capture.get("tail_bytes"),
                    label="capture.tail_bytes",
                    # Two base64 tails plus runtime metadata must fit inside
                    # BackendOutcome's one-MiB evidence envelope.
                    maximum=MAXIMUM_CAPTURE_TAIL_BYTES,
                ),
            )
        except ValueError as error:
            raise CommandBackendError(
                "MALFORMED_COMMAND_CONFIG", "capture"
            ) from error
        return cls(
            name=name,
            argv=tuple(argv),
            result_path=result_path,
            capture_limits=capture_limits,
            executable_sha256=executable_sha256,
            _executable_identity=executable_identity,
        )

    def to_public_value(self) -> dict[str, object]:
        return {
            "schema": COMMAND_BACKEND_CONFIG_SCHEMA,
            "argv": list(self.argv),
            "argv_is_public": True,
            "executable_sha256": self.executable_sha256,
            "result_path": self.result_path,
            "capture": {
                "maximum_retained_bytes": self.capture_limits.maximum_retained_bytes,
                "maximum_observed_bytes": self.capture_limits.maximum_observed_bytes,
                "tail_bytes": self.capture_limits.tail_bytes,
            },
            "environment_names": sorted(_SESSION_ENVIRONMENT_NAMES),
            "containment": "COOPERATIVE_PROCESS_GROUP",
            "maximum_executable_bytes": MAXIMUM_EXECUTABLE_BYTES,
        }

    def verify_executable(self) -> None:
        digest, identity = _hash_executable(Path(self.argv[0]))
        if digest != self.executable_sha256 or identity != self._executable_identity:
            _fail("COMMAND_EXECUTABLE_DRIFT")


def load_command_backend_config(path: Path) -> CommandBackendConfig:
    """Load one strict bounded JSON backend configuration file."""

    if not isinstance(path, Path):
        raise TypeError("path must be Path")
    selected = path if path.is_absolute() else path.absolute()
    raw = _read_regular_file(
        selected,
        maximum_bytes=MAXIMUM_CONFIG_BYTES,
        code="COMMAND_CONFIG_UNREADABLE",
    )
    return CommandBackendConfig.from_value(
        _strict_json(raw, maximum_bytes=MAXIMUM_CONFIG_BYTES, label="config")
    )


def _require_exact_directory(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        _fail("COMMAND_SESSION_LAYOUT_INVALID", label)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CommandBackendError(
            "COMMAND_SESSION_LAYOUT_INVALID", label
        ) from error
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        _fail("COMMAND_SESSION_LAYOUT_INVALID", label)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CommandBackendError(
            "COMMAND_SESSION_LAYOUT_INVALID", label
        ) from error
    if resolved != path:
        _fail("COMMAND_SESSION_LAYOUT_INVALID", f"{label}: noncanonical")
    return resolved


def _require_nonoverlapping_session_directories(request: SessionRequest) -> None:
    selected = {
        label: _require_exact_directory(getattr(request, label), label=label)
        for label in ("private_root", "workspace", "cache", "evidence")
    }
    rows = list(selected.items())
    for index, (left_name, left) in enumerate(rows):
        for right_name, right in rows[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                _fail(
                    "COMMAND_SESSION_LAYOUT_INVALID",
                    f"{left_name}/{right_name} overlap",
                )


def _mkdir_private(root: Path, *components: str) -> Path:
    selected = root.joinpath(*components)
    selected.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = _require_exact_directory(selected, label="runtime private directory")
    try:
        os.chmod(resolved, 0o700)
    except OSError as error:
        raise CommandBackendError(
            "COMMAND_SESSION_LAYOUT_INVALID", "chmod"
        ) from error
    return resolved


def _result_path(workspace: Path, relative: str) -> Path:
    selected = workspace.joinpath(*PurePath(relative).parts)
    try:
        parent = selected.parent.resolve(strict=True)
    except OSError as error:
        raise CommandBackendError("COMMAND_RESULT_PATH_INVALID", "parent") from error
    if parent != workspace and workspace not in parent.parents:
        _fail("COMMAND_RESULT_PATH_INVALID", "escape")
    if selected.exists() or selected.is_symlink():
        _fail("COMMAND_RESULT_PATH_INVALID", "already exists")
    return selected


def _open_exclusive_evidence(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags, 0o600)
    except OSError as error:
        raise CommandBackendError(
            "COMMAND_EVIDENCE_CREATE_FAILED", path.name
        ) from error


def _write_all(file_descriptor: int, raw: bytes) -> None:
    selected = memoryview(raw)
    while selected:
        written = os.write(file_descriptor, selected)
        if written <= 0:
            raise OSError("short evidence write")
        selected = selected[written:]


class _StreamCapture:
    def __init__(self, *, name: str, path: Path, limits: CaptureLimits) -> None:
        self.name = name
        self.path = path
        # The prefix is already streamed into the exclusive evidence file;
        # retaining the same bytes in the accumulator would double memory.
        self.accumulator = BoundedCaptureAccumulator(
            limits, retain_content=False
        )
        self.file_descriptor = _open_exclusive_evidence(path)
        self.retained_file_bytes: int | None = None
        self.error: str | None = None
        self._closed = False

    async def drain(
        self,
        stream: asyncio.StreamReader,
        cap_event: asyncio.Event,
        failure_event: asyncio.Event,
    ) -> None:
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                return
            outcome = self.accumulator.append(chunk)
            if outcome.retained_bytes_added:
                try:
                    _write_all(
                        self.file_descriptor,
                        chunk[: outcome.retained_bytes_added],
                    )
                except OSError:
                    if self.error is None:
                        self.error = "evidence write failed"
                        failure_event.set()
            if outcome.safety_cap_crossed:
                cap_event.set()

    def finalize(self) -> CaptureSnapshot:
        if not self._closed:
            try:
                os.fsync(self.file_descriptor)
            except OSError:
                if self.error is None:
                    self.error = "evidence fsync failed"
            try:
                self.retained_file_bytes = os.fstat(self.file_descriptor).st_size
            except OSError:
                if self.error is None:
                    self.error = "evidence stat failed"
            finally:
                os.close(self.file_descriptor)
                self._closed = True
        return self.accumulator.finalize()

    def abort(self) -> None:
        if not self._closed:
            os.close(self.file_descriptor)
            self._closed = True


_SESSION_ENVIRONMENT_NAMES = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "PIP_CACHE_DIR",
        "PYTHONPYCACHEPREFIX",
        "NPM_CONFIG_CACHE",
        "YARN_CACHE_FOLDER",
        "GOCACHE",
        "GOMODCACHE",
        "CCACHE_DIR",
        "CLANG_MODULE_CACHE_PATH",
        "SWIFTPM_MODULECACHE_OVERRIDE",
        "SWIFT_MODULECACHE_PATH",
        "PMW_SESSION_ID",
        "PMW_COHORT_ID",
        "PMW_WORLD_ID",
        "PMW_PLAN_SHA256",
        "PMW_LAUNCH_SHA256",
        "PMW_BRIEFING_PATH",
        "PMW_INVOCATION_PATH",
        "PMW_WORKSPACE",
        "PMW_CACHE",
        "PMW_RESULT_PATH",
    }
)


def _session_environment(request: SessionRequest, result_path: Path) -> dict[str, str]:
    private = request.private_root
    cache = request.cache
    home = _mkdir_private(private, "home")
    temporary = _mkdir_private(private, "tmp")
    xdg_config = _mkdir_private(private, "xdg-config")
    xdg_data = _mkdir_private(private, "xdg-data")
    xdg_state = _mkdir_private(private, "xdg-state")
    xdg_cache = _mkdir_private(cache, "xdg")
    compiler_cache = _mkdir_private(cache, "compiler")
    swift_cache = _mkdir_private(cache, "swift")
    return {
        # These constants are protocol behavior, not inherited host values.
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
        "CARGO_HOME": str(_mkdir_private(cache, "cargo")),
        "RUSTUP_HOME": str(_mkdir_private(cache, "rustup")),
        "PIP_CACHE_DIR": str(_mkdir_private(cache, "pip")),
        "PYTHONPYCACHEPREFIX": str(_mkdir_private(cache, "pycache")),
        "NPM_CONFIG_CACHE": str(_mkdir_private(cache, "npm")),
        "YARN_CACHE_FOLDER": str(_mkdir_private(cache, "yarn")),
        "GOCACHE": str(_mkdir_private(cache, "go-build")),
        "GOMODCACHE": str(_mkdir_private(cache, "go-mod")),
        "CCACHE_DIR": str(compiler_cache),
        "CLANG_MODULE_CACHE_PATH": str(compiler_cache),
        "SWIFTPM_MODULECACHE_OVERRIDE": str(swift_cache),
        "SWIFT_MODULECACHE_PATH": str(swift_cache),
        "PMW_SESSION_ID": request.spec.session_id,
        "PMW_COHORT_ID": request.spec.cohort_id,
        "PMW_WORLD_ID": request.spec.world_id,
        "PMW_PLAN_SHA256": request.plan_sha256,
        "PMW_LAUNCH_SHA256": request.launch_sha256,
        "PMW_BRIEFING_PATH": str(request.briefing_path),
        "PMW_INVOCATION_PATH": str(request.invocation_path),
        "PMW_WORKSPACE": str(request.workspace),
        "PMW_CACHE": str(request.cache),
        "PMW_RESULT_PATH": str(result_path),
    }


def _group_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_group_exit(process_group_id: int, seconds: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, seconds)
    while _group_alive(process_group_id):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(min(0.025, max(0.0, deadline - loop.time())))
    return True


async def _terminate_cooperative_group(
    process_group_id: int,
    *,
    reason: str,
    grace_seconds: float,
) -> StopProof:
    alive = _group_alive(process_group_id)
    forced = False
    detail = "cooperative process group was already absent"
    if alive:
        detail = "SIGTERM sent to cooperative process group"
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            alive = False
        if alive:
            alive = not await _wait_for_group_exit(process_group_id, grace_seconds)
    if alive:
        forced = True
        detail = "SIGKILL sent to cooperative process group"
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            alive = False
        if alive:
            alive = not await _wait_for_group_exit(
                process_group_id, min(max(grace_seconds, 0.1), 5.0)
            )
    stopped = not alive and not _group_alive(process_group_id)
    if not stopped:
        detail = "cooperative process group could not be proven absent"
    return StopProof(
        stopped=stopped,
        reason=reason,
        forced=forced,
        process_group_id=process_group_id,
        detail=detail,
    )


def _capture_value(
    name: str,
    snapshot: CaptureSnapshot,
    *,
    retained_file_bytes: int | None,
) -> dict[str, object]:
    return {
        "retained_file": f"{name}.retained.bin",
        "observed_bytes": snapshot.observed_bytes,
        "retained_bytes": snapshot.retained_bytes,
        "retained_file_bytes": retained_file_bytes,
        "retained_content_in_snapshot": snapshot.retained_content_in_snapshot,
        "retained_storage": (
            "EVIDENCE_FILE_AND_SNAPSHOT"
            if snapshot.retained_content_in_snapshot
            else "EVIDENCE_FILE"
        ),
        "observed_sha256": snapshot.observed_sha256,
        "tail_base64": base64.b64encode(snapshot.tail).decode("ascii"),
        "truncated": snapshot.truncated,
        "observed_safety_cap_exceeded": snapshot.observed_safety_cap_exceeded,
    }


class _RunningCommandSession:
    def __init__(
        self,
        *,
        config: CommandBackendConfig,
        request: SessionRequest,
        process: asyncio.subprocess.Process,
        result_path: Path,
        stdout_capture: _StreamCapture,
        stderr_capture: _StreamCapture,
    ) -> None:
        if process.stdout is None or process.stderr is None:
            raise AssertionError("command pipes are absent")
        self._config = config
        self._request = request
        self._process = process
        self._process_group_id = process.pid
        self._result_path = result_path
        self._stdout = stdout_capture
        self._stderr = stderr_capture
        self._cap_event = asyncio.Event()
        self._capture_failure_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._requested_stop_reason: str | None = None
        self._termination_lock = asyncio.Lock()
        self._stop_proof: StopProof | None = None
        self._cleanup_had_residual = False
        self._stdout_task = asyncio.create_task(
            stdout_capture.drain(
                process.stdout, self._cap_event, self._capture_failure_event
            ),
            name=f"{request.spec.session_id}:stdout",
        )
        self._stderr_task = asyncio.create_task(
            stderr_capture.drain(
                process.stderr, self._cap_event, self._capture_failure_event
            ),
            name=f"{request.spec.session_id}:stderr",
        )
        self._completion = asyncio.create_task(
            self._run_guarded(), name=f"{request.spec.session_id}:command"
        )

    async def wait(self) -> BackendOutcome:
        return await asyncio.shield(self._completion)

    async def stop(self, reason: str, grace_seconds: float) -> StopProof:
        if type(reason) is not str or _REASON.fullmatch(reason) is None:
            raise RuntimeContractError("MALFORMED_STOP_PROOF", "reason")
        if isinstance(grace_seconds, bool) or not isinstance(
            grace_seconds, (int, float)
        ):
            raise TypeError("grace_seconds must be numeric")
        if grace_seconds < 0 or grace_seconds > MAXIMUM_STOP_GRACE_SECONDS:
            raise ValueError("grace_seconds is out of bounds")

        # Once the leader has exited, its reported completion wins.  Cleanup
        # may still terminate residual members of the cooperative group.
        if self._process.returncode is not None and not self._stop_event.is_set():
            proof = await self._ensure_group_stopped(
                "PROCESS_EXIT", float(grace_seconds), natural_exit=True
            )
        else:
            if self._requested_stop_reason is None:
                self._requested_stop_reason = reason
                self._stop_event.set()
            proof = await self._ensure_group_stopped(
                self._requested_stop_reason,
                float(grace_seconds),
                natural_exit=False,
            )

        # Process absence is necessary but not sufficient: the two drainers
        # must also reach EOF and fsync their bounded evidence before the host
        # may persist a terminal receipt.  ``_run`` calls the private cleanup
        # primitive directly, so waiting here cannot self-deadlock.
        if not self._completion.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._completion),
                    timeout=min(max(float(grace_seconds), 1.0), 5.0) + 6.0,
                )
            except TimeoutError as error:
                # The handle contract does not permit a returned stop proof
                # while adapter-owned cleanup continues invisibly.  Cancellation
                # runs ``_run_guarded``'s bounded process/drainer cleanup before
                # this method returns the negative proof.
                self._completion.cancel()
                await asyncio.gather(self._completion, return_exceptions=True)
                return StopProof(
                    stopped=False,
                    reason="BACKEND_SETTLEMENT_UNPROVEN",
                    forced=proof.forced,
                    process_group_id=self._process_group_id,
                    detail=f"bounded evidence did not settle: {type(error).__name__}",
                )
        return proof

    async def _ensure_group_stopped(
        self,
        reason: str,
        grace_seconds: float,
        *,
        natural_exit: bool,
    ) -> StopProof:
        async with self._termination_lock:
            if self._stop_proof is not None:
                return self._stop_proof
            alive = _group_alive(self._process_group_id)
            if natural_exit and alive:
                self._cleanup_had_residual = True
            self._stop_proof = await _terminate_cooperative_group(
                self._process_group_id,
                reason=reason,
                grace_seconds=grace_seconds,
            )
            return self._stop_proof

    async def _run_guarded(self) -> BackendOutcome:
        try:
            return await self._run()
        except asyncio.CancelledError:
            await self._ensure_group_stopped(
                "RUNTIME_CANCELLED",
                float(self._request.stop_grace_seconds),
                natural_exit=False,
            )
            await self._finish_drainers()
            self._finalize_captures()
            raise
        except Exception as error:
            proof = await self._ensure_group_stopped(
                "BACKEND_RUNTIME_FAILURE",
                float(self._request.stop_grace_seconds),
                natural_exit=False,
            )
            await self._finish_drainers()
            captures = self._finalize_captures()
            return BackendOutcome(
                success=False,
                terminal_reason="BACKEND_RUNTIME_FAILURE",
                summary=f"local command adapter failed: {type(error).__name__}",
                evidence={
                    "command_runtime": {
                        "containment": "COOPERATIVE_PROCESS_GROUP",
                        "stop_proof": proof.to_value(),
                        "captures": captures,
                    }
                },
            )

    async def _run(self) -> BackendOutcome:
        # ``asyncio.subprocess.Process.wait`` may not resolve until inherited
        # stdout/stderr descriptors close.  A residual descendant can keep
        # those descriptors open after the leader exits, so monitor the child
        # watcher's returncode directly and then clean the process group.
        leader_wait = asyncio.create_task(self._wait_for_leader_exit())
        cap_wait = asyncio.create_task(self._cap_event.wait())
        capture_failure_wait = asyncio.create_task(
            self._capture_failure_event.wait()
        )
        stop_wait = asyncio.create_task(self._stop_event.wait())
        waiters: set[asyncio.Task[object]] = {
            leader_wait,
            cap_wait,
            capture_failure_wait,
            stop_wait,
        }
        done, pending = await asyncio.wait(
            waiters, return_when=asyncio.FIRST_COMPLETED
        )

        terminal_reason: str | None = None
        natural_exit = leader_wait in done
        if self._cap_event.is_set():
            terminal_reason = "OUTPUT_SAFETY_CAP"
            natural_exit = False
        elif self._capture_failure_event.is_set():
            terminal_reason = "OUTPUT_CAPTURE_FAILED"
            natural_exit = False
        elif self._stop_event.is_set():
            terminal_reason = self._requested_stop_reason or "STOP_REQUESTED"
            natural_exit = False

        for waiter in pending:
            waiter.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        proof = await self._ensure_group_stopped(
            "PROCESS_EXIT" if natural_exit else terminal_reason or "STOP_REQUESTED",
            float(self._request.stop_grace_seconds),
            natural_exit=natural_exit,
        )
        if self._process.returncode is None:
            try:
                await asyncio.wait_for(
                    self._process.wait(),
                    timeout=min(max(self._request.stop_grace_seconds, 1), 5),
                )
            except TimeoutError:
                pass
        await self._finish_drainers()
        captures = self._finalize_captures()
        # A short-lived command may exit before the pipe drainers observe the
        # final buffered chunk.  Classification therefore consults their
        # terminal state again after both streams reach EOF.
        if self._cap_event.is_set():
            terminal_reason = "OUTPUT_SAFETY_CAP"
        elif (
            self._capture_failure_event.is_set()
            or self._stdout.error is not None
            or self._stderr.error is not None
        ):
            terminal_reason = "OUTPUT_CAPTURE_FAILED"
        runtime_evidence: dict[str, object] = {
            "containment": "COOPERATIVE_PROCESS_GROUP",
            "process_group_id": self._process_group_id,
            "exit_code": self._process.returncode,
            "stop_proof": proof.to_value(),
            "captures": captures,
        }

        if not proof.stopped:
            return self._failure(
                "PROCESS_GROUP_CLEANUP_FAILED",
                "process group could not be proven stopped",
                runtime_evidence,
            )
        if natural_exit and self._cleanup_had_residual:
            return self._failure(
                "PROCESS_GROUP_RESIDUAL",
                "leader exited while its cooperative process group remained active",
                runtime_evidence,
            )
        if terminal_reason is not None:
            return self._failure(
                terminal_reason,
                "local command was stopped by the runtime",
                runtime_evidence,
            )
        if self._process.returncode != 0:
            return self._failure(
                "COMMAND_EXIT_NONZERO",
                f"local command exited with status {self._process.returncode}",
                runtime_evidence,
            )

        try:
            raw_result = _read_regular_file(
                self._result_path,
                maximum_bytes=MAXIMUM_RESULT_BYTES,
                code="COMMAND_RESULT_INVALID",
            )
            value = _strict_json(
                raw_result,
                maximum_bytes=MAXIMUM_RESULT_BYTES,
                label="result",
            )
            reported = BackendOutcome.from_value(value)
            reported_evidence = reported.evidence
            if "command_runtime" in reported_evidence:
                _fail("COMMAND_RESULT_INVALID", "reserved evidence field")
            reported_evidence["command_runtime"] = runtime_evidence
            return BackendOutcome(
                success=reported.success,
                terminal_reason=reported.terminal_reason,
                summary=reported.summary,
                contributions=reported.contributions,
                usage=reported.usage,
                evidence=reported_evidence,
            )
        except (CommandBackendError, RuntimeContractError, ValueError) as error:
            return self._failure(
                "COMMAND_RESULT_INVALID",
                f"local command result was rejected: {type(error).__name__}",
                runtime_evidence,
            )

    async def _wait_for_leader_exit(self) -> None:
        while self._process.returncode is None:
            await asyncio.sleep(0.025)

    async def _finish_drainers(self) -> None:
        combined = asyncio.gather(
            self._stdout_task, self._stderr_task, return_exceptions=True
        )
        try:
            results = await asyncio.wait_for(
                combined,
                timeout=min(max(self._request.stop_grace_seconds, 1), 5),
            )
        except TimeoutError:
            self._stdout_task.cancel()
            self._stderr_task.cancel()
            await asyncio.gather(
                self._stdout_task, self._stderr_task, return_exceptions=True
            )
            if self._stdout.error is None:
                self._stdout.error = "pipe did not close after process-group cleanup"
            if self._stderr.error is None:
                self._stderr.error = "pipe did not close after process-group cleanup"
            self._capture_failure_event.set()
        else:
            for selected, result in zip((self._stdout, self._stderr), results):
                if isinstance(result, BaseException):
                    selected.error = f"pipe drain failed: {type(result).__name__}"
                    self._capture_failure_event.set()

    def _finalize_captures(self) -> dict[str, object]:
        stdout = self._stdout.finalize()
        stderr = self._stderr.finalize()
        value: dict[str, object] = {
            "stdout": _capture_value(
                "stdout",
                stdout,
                retained_file_bytes=self._stdout.retained_file_bytes,
            ),
            "stderr": _capture_value(
                "stderr",
                stderr,
                retained_file_bytes=self._stderr.retained_file_bytes,
            ),
        }
        errors = {
            name: selected.error
            for name, selected in (("stdout", self._stdout), ("stderr", self._stderr))
            if selected.error is not None
        }
        if errors:
            value["errors"] = errors
        return value

    @staticmethod
    def _failure(
        reason: str,
        summary: str,
        runtime_evidence: dict[str, object],
    ) -> BackendOutcome:
        return BackendOutcome(
            success=False,
            terminal_reason=reason,
            summary=summary,
            evidence={"command_runtime": runtime_evidence},
        )


class CommandBackend:
    """Run canonical local commands behind the generic backend protocol."""

    def __init__(self, config: CommandBackendConfig) -> None:
        if not isinstance(config, CommandBackendConfig):
            raise TypeError("config must be CommandBackendConfig")
        self._config = config
        self._identity = BackendIdentity(
            name=config.name,
            protocol=COMMAND_BACKEND_PROTOCOL,
            public_config=config.to_public_value(),
        )

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    async def start(self, request: SessionRequest) -> _RunningCommandSession:
        if not isinstance(request, SessionRequest):
            raise TypeError("request must be SessionRequest")
        stdout_capture: _StreamCapture | None = None
        stderr_capture: _StreamCapture | None = None
        spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None
        process: asyncio.subprocess.Process | None = None
        handed_off = False
        try:
            _require_nonoverlapping_session_directories(request)
            try:
                executable = await _materialize_executable(
                    self._config, request.private_root
                )
            except asyncio.CancelledError as cancelled:
                raise BackendStartError(
                    "COMMAND_START_CANCELLED",
                    stop_proof=StopProof(
                        stopped=True,
                        reason="START_CANCELLED",
                        detail="cancelled before command process creation",
                    ),
                ) from cancelled
            result_path = _result_path(request.workspace, self._config.result_path)
            stdout_capture = _StreamCapture(
                name="stdout",
                path=request.evidence / "stdout.retained.bin",
                limits=self._config.capture_limits,
            )
            try:
                stderr_capture = _StreamCapture(
                    name="stderr",
                    path=request.evidence / "stderr.retained.bin",
                    limits=self._config.capture_limits,
                )
            except Exception:
                stdout_capture.abort()
                try:
                    (request.evidence / "stdout.retained.bin").unlink()
                except OSError:
                    pass
                raise
            environment = _session_environment(request, result_path)
            spawn_task = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    str(executable),
                    *self._config.argv[1:],
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=request.workspace,
                    env=environment,
                    start_new_session=True,
                )
            )
            try:
                process = await asyncio.shield(spawn_task)
            except asyncio.CancelledError as cancelled:
                # Close the create-process cancellation gap: if spawning won
                # the race, obtain a handle and prove its process group gone.
                try:
                    process = await spawn_task
                except Exception as error:
                    stdout_capture.abort()
                    stderr_capture.abort()
                    raise BackendStartError(
                        "COMMAND_START_CANCELLED",
                        stop_proof=StopProof(
                            stopped=True,
                            reason="START_CANCELLED",
                            detail="process creation did not complete",
                        ),
                    ) from error
                running = _RunningCommandSession(
                    config=self._config,
                    request=request,
                    process=process,
                    result_path=result_path,
                    stdout_capture=stdout_capture,
                    stderr_capture=stderr_capture,
                )
                proof = await asyncio.shield(
                    running.stop(
                        "START_CANCELLED", float(request.stop_grace_seconds)
                    )
                )
                raise BackendStartError(
                    "COMMAND_START_CANCELLED", stop_proof=proof
                ) from cancelled
            running = _RunningCommandSession(
                config=self._config,
                request=request,
                process=process,
                result_path=result_path,
                stdout_capture=stdout_capture,
                stderr_capture=stderr_capture,
            )
            handed_off = True
            return running
        except BackendStartError:
            raise
        except Exception as error:
            if not handed_off:
                if stdout_capture is not None:
                    stdout_capture.abort()
                if stderr_capture is not None:
                    stderr_capture.abort()
            if process is not None and not handed_off:
                proof = await _terminate_cooperative_group(
                    process.pid,
                    reason="START_FAILED",
                    grace_seconds=float(request.stop_grace_seconds),
                )
            else:
                proof = StopProof(
                    stopped=True,
                    reason="START_FAILED",
                    detail="no command process was created",
                )
            raise BackendStartError(
                "COMMAND_START_FAILED",
                type(error).__name__,
                stop_proof=proof,
            ) from error


def load_command_backend(path: Path) -> CommandBackend:
    """Construct a command backend from one strict JSON configuration file."""

    return CommandBackend(load_command_backend_config(path))
