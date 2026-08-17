"""Standalone in-session runner shipped inside the pinned AMF verifier kit.

These exact bytes are materialized read-only into a research workspace as
``<kit>/lib/amf_verify.py`` and executed by ``<kit>/bin/amf-verify``.  The
module therefore uses only the standard library and never imports
``pmw_platform``: it runs under ``python -I`` from a workspace path, and the
platform package must not become an in-session dependency.

Its verdicts are **advisory**.  ``AmfVerifierService`` re-executing the same
pinned verifier after settlement remains the sole authority; nothing written
here is an admission, a novelty claim, or a solved open problem.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time


KIT_MANIFEST_SCHEMA = "PMW_IN_SESSION_VERIFIER_KIT_1"
KIT_BINDINGS_SCHEMA = "PMW_IN_SESSION_VERIFIER_KIT_BINDINGS_1"
VERDICT_SCHEMA = "PMW_IN_SESSION_VERIFIER_VERDICT_1"
INVOCATION_SCHEMA = "PMW_IN_SESSION_VERIFIER_INVOCATION_1"
IN_SESSION_AUTHORITY = "ADVISORY_IN_SESSION_VERIFICATION"
SETTLEMENT_AUTHORITY = "HOST_REEXECUTED_PINNED_AMF_VERIFIER"
VERIFIER_RESULT_SCHEMA = "AMF_VERIFIER_RESULT_1"
EVIDENCE_DIRECTORY_NAME = ".pmw-verifier-evidence"

MAXIMUM_BINDINGS_BYTES = 16 * 1024 * 1024
MAXIMUM_MANIFEST_BYTES = 16 * 1024 * 1024
MAXIMUM_SOURCE_ARTIFACT_BYTES = 1_073_741_824
MAXIMUM_CANDIDATE_BYTES = 1_073_741_824
MAXIMUM_INTERPRETER_BYTES = 1_073_741_824
MAXIMUM_INLINE_VERIFIER_RESULT_BYTES = 32_768
MAXIMUM_INVOCATIONS = 100_000

EXIT_PASS = 0
EXIT_REJECTED = 1
EXIT_APPARATUS_ERROR = 2
EXIT_USAGE = 64

CLAIM_CEILING = (
    "Advisory in-session evidence produced inside the research workspace. "
    "Only the host's post-settlement re-execution of the same pinned verifier "
    "is authoritative; no verifier result asserts novelty or a solved open "
    "problem."
)


class KitError(Exception):
    """A stable in-session apparatus failure with a machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8", errors="strict")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_object(raw: bytes, *, code: str) -> dict[str, object]:
    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        selected: dict[str, object] = {}
        for key, value in pairs:
            if key in selected:
                raise ValueError("duplicate key")
            selected[key] = value
        return selected

    def reject_number(_value: str) -> object:
        raise ValueError("floating-point JSON is unsupported")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise KitError(code) from error
    if type(value) is not dict:
        raise KitError(code, "root must be an object")
    return value


def _stable_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_relative_nofollow(root: Path, relative: str, *, code: str) -> int:
    """Open one workspace-relative regular file without traversing a symlink."""

    parts = PurePosixPath(relative).parts
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        current = os.open(root, flags)
    except OSError as error:
        raise KitError(code, relative) from error
    try:
        for part in parts[:-1]:
            following = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = following
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        return os.open(parts[-1], file_flags, dir_fd=current)
    except OSError as error:
        raise KitError(code, relative) from error
    finally:
        os.close(current)


def _read_stable_regular(
    descriptor: int,
    *,
    maximum_bytes: int,
    code: str,
    detail: str = "",
) -> bytes:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= maximum_bytes:
        raise KitError(code, detail)
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
        raise KitError(code, detail or "unstable file")
    return raw


def _read_kit_file(kit_root: Path, relative: str, *, maximum_bytes: int) -> bytes:
    descriptor = _open_relative_nofollow(
        kit_root, relative, code="KIT_FILE_UNAVAILABLE"
    )
    try:
        return _read_stable_regular(
            descriptor,
            maximum_bytes=maximum_bytes,
            code="KIT_FILE_UNAVAILABLE",
            detail=relative,
        )
    finally:
        os.close(descriptor)


def _hash_absolute_regular(path: Path, *, maximum_bytes: int, code: str) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise KitError(code, str(path)) from error
    digest = hashlib.sha256()
    count = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum_bytes:
            raise KitError(code, str(path))
        while True:
            chunk = os.read(descriptor, 1_048_576)
            if not chunk:
                break
            count += len(chunk)
            if count > maximum_bytes:
                raise KitError(code, str(path))
            digest.update(chunk)
        after = os.fstat(descriptor)
        if count != before.st_size or _stable_metadata(before) != _stable_metadata(after):
            raise KitError(code, "file changed while hashing")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), count


def _relative_kit_path(value: object, *, code: str) -> str:
    if type(value) is not str or not value or "\x00" in value or "\\" in value:
        raise KitError(code)
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise KitError(code)
    return value


def _load_kit(kit_root: Path) -> tuple[dict[str, object], dict[str, object], str, str]:
    """Read the kit manifest and bindings, checking every declared digest."""

    manifest_raw = _read_kit_file(
        kit_root, "manifest.json", maximum_bytes=MAXIMUM_MANIFEST_BYTES
    )
    manifest = _strict_object(manifest_raw, code="KIT_MANIFEST_INVALID")
    if manifest.get("schema") != KIT_MANIFEST_SCHEMA:
        raise KitError("KIT_MANIFEST_INVALID", "schema")
    files = manifest.get("files")
    if type(files) is not list or not files:
        raise KitError("KIT_MANIFEST_INVALID", "files")
    bindings_digest: str | None = None
    for row in files:
        if type(row) is not dict or set(row) != {"path", "bytes", "sha256", "mode"}:
            raise KitError("KIT_MANIFEST_INVALID", "file row")
        path = _relative_kit_path(row.get("path"), code="KIT_MANIFEST_INVALID")
        if path == "lib/bindings.json":
            bindings_digest = str(row.get("sha256"))
    if bindings_digest is None:
        raise KitError("KIT_MANIFEST_INVALID", "bindings row")
    bindings_raw = _read_kit_file(
        kit_root, "lib/bindings.json", maximum_bytes=MAXIMUM_BINDINGS_BYTES
    )
    if hashlib.sha256(bindings_raw).hexdigest() != bindings_digest:
        raise KitError("KIT_BINDINGS_PIN_MISMATCH")
    bindings = _strict_object(bindings_raw, code="KIT_BINDINGS_INVALID")
    if (
        bindings.get("schema") != KIT_BINDINGS_SCHEMA
        or bindings.get("authority") != IN_SESSION_AUTHORITY
        or type(bindings.get("targets")) is not list
        or not bindings["targets"]
    ):
        raise KitError("KIT_BINDINGS_INVALID", "envelope")
    content_sha256 = manifest.get("content_sha256")
    if type(content_sha256) is not str:
        raise KitError("KIT_MANIFEST_INVALID", "content_sha256")
    return (
        manifest,
        bindings,
        hashlib.sha256(manifest_raw).hexdigest(),
        content_sha256,
    )


def _select_target(
    bindings: dict[str, object], target_id: str | None
) -> dict[str, object]:
    rows = [row for row in bindings["targets"] if type(row) is dict]  # type: ignore[index]
    known = [str(row.get("target_id")) for row in rows]
    if target_id is None:
        if len(rows) != 1:
            raise KitError(
                "TARGET_SELECTION_REQUIRED",
                f"choose --target from: {', '.join(sorted(known))}",
            )
        return rows[0]
    for row in rows:
        if row.get("target_id") == target_id:
            return row
    raise KitError(
        "UNKNOWN_TARGET", f"{target_id}; known targets: {', '.join(sorted(known))}"
    )


def _workspace_relative(workspace: Path, supplied: str) -> str:
    if not supplied or "\x00" in supplied:
        raise KitError("UNSAFE_CANDIDATE_PATH", "empty or NUL path")
    base = Path(supplied)
    if not base.is_absolute():
        base = Path(os.getcwd()) / base
    normalized = Path(os.path.normpath(str(base)))
    try:
        relative = normalized.relative_to(workspace)
    except ValueError as error:
        raise KitError(
            "UNSAFE_CANDIDATE_PATH",
            "the candidate must be inside the session workspace",
        ) from error
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise KitError("UNSAFE_CANDIDATE_PATH", supplied[:512])
    return PurePosixPath(*parts).as_posix()


def _capture_candidate(workspace: Path, relative: str) -> dict[str, object]:
    descriptor = _open_relative_nofollow(
        workspace, relative, code="UNSAFE_CANDIDATE_PATH"
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > MAXIMUM_CANDIDATE_BYTES
        ):
            raise KitError("CANDIDATE_SIZE_OR_TYPE_INVALID", relative)
        digest = hashlib.sha256()
        count = 0
        while True:
            chunk = os.read(descriptor, 1_048_576)
            if not chunk:
                break
            count += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if count != before.st_size or _stable_metadata(before) != _stable_metadata(after):
            raise KitError("CANDIDATE_CHANGED_DURING_READ", relative)
    finally:
        os.close(descriptor)
    return {
        "workspace_relative_path": relative,
        "sha256": digest.hexdigest(),
        "bytes": count,
    }


def _write_exact(path: Path, raw: bytes, mode: int = 0o400) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, mode)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise KitError("KIT_EXECUTION_TREE_FAILED", str(path))
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_source(
    kit_root: Path,
    target: dict[str, object],
    destination: Path,
) -> str:
    """Copy the pinned verifier source, re-checking every declared digest."""

    verifier_id = str(target.get("verifier_id"))
    artifacts = target.get("source_artifacts")
    if type(artifacts) is not list or not artifacts:
        raise KitError("KIT_BINDINGS_INVALID", "source_artifacts")
    closure: list[dict[str, object]] = []
    for row in artifacts:
        if type(row) is not dict or set(row) != {"path", "bytes", "sha256"}:
            raise KitError("KIT_BINDINGS_INVALID", "source artifact row")
        path = _relative_kit_path(row.get("path"), code="KIT_BINDINGS_INVALID")
        expected_bytes = row.get("bytes")
        expected_digest = row.get("sha256")
        if type(expected_bytes) is not int or type(expected_digest) is not str:
            raise KitError("KIT_BINDINGS_INVALID", "source artifact identity")
        raw = _read_kit_file(
            kit_root,
            f"source/{verifier_id}/{path}",
            maximum_bytes=MAXIMUM_SOURCE_ARTIFACT_BYTES,
        )
        if len(raw) != expected_bytes or hashlib.sha256(raw).hexdigest() != expected_digest:
            raise KitError("KIT_SOURCE_PIN_MISMATCH", path)
        _write_exact(destination.joinpath(*PurePosixPath(path).parts), raw)
        closure.append(
            {"path": path, "bytes": expected_bytes, "sha256": expected_digest}
        )
    return hashlib.sha256(_canonical_json(closure)).hexdigest()


def _group_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_group(process: "subprocess.Popen[bytes]", grace_seconds: float) -> bool:
    group = process.pid
    if not _group_alive(group):
        return True
    for number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, number)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            process.poll()
            if not _group_alive(group):
                return True
            time.sleep(0.01)
    return not _group_alive(group)


def _run_bounded(
    *,
    command: list[str],
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    output_cap: int,
) -> dict[str, object]:
    started = time.monotonic_ns()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        return {
            "state": "SPAWN_FAILED",
            "exit_code": None,
            "signal": None,
            "elapsed_ms": 0,
            "stdout": b"",
            "stderr": b"",
            "stdout_observed_bytes": 0,
            "stderr_observed_bytes": 0,
            "cleanup_complete": True,
        }
    assert process.stdout is not None and process.stderr is not None
    lock = threading.Lock()
    overflow = threading.Event()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    observed = {"stdout": 0, "stderr": 0}
    failures: list[str] = []

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
            failures.append(label)

    threads = [
        threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout_seconds
    state: str | None = None
    cleanup_complete = True
    while process.poll() is None:
        if overflow.is_set():
            state = "OUTPUT_LIMIT_EXCEEDED"
        elif time.monotonic() >= deadline:
            state = "TIMEOUT"
        if state is not None:
            cleanup_complete = _terminate_group(process, 2.0)
            break
        overflow.wait(0.01)
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        cleanup_complete = _terminate_group(process, 2.0) and cleanup_complete
    if _group_alive(process.pid):
        if state is None:
            state = "PROCESS_GROUP_LEAK"
        cleanup_complete = _terminate_group(process, 2.0) and cleanup_complete
    for thread in threads:
        thread.join(timeout=2.0)
    if any(thread.is_alive() for thread in threads):
        cleanup_complete = False
        state = "PROCESS_GROUP_CLEANUP_FAILED"
    process.stdout.close()
    process.stderr.close()
    if failures:
        state = "OUTPUT_CAPTURE_FAILED"
    if overflow.is_set() and state is None:
        state = "OUTPUT_LIMIT_EXCEEDED"
    if not cleanup_complete:
        state = "PROCESS_GROUP_CLEANUP_FAILED"
    if state is None:
        state = "COMPLETED"
    signal_name = None
    if process.returncode is not None and process.returncode < 0:
        try:
            signal_name = signal.Signals(-process.returncode).name
        except ValueError:
            signal_name = f"SIG{-process.returncode}"
    return {
        "state": state,
        "exit_code": process.returncode,
        "signal": signal_name,
        "elapsed_ms": max(0, (time.monotonic_ns() - started) // 1_000_000),
        "stdout": bytes(buffers["stdout"]),
        "stderr": bytes(buffers["stderr"]),
        "stdout_observed_bytes": observed["stdout"],
        "stderr_observed_bytes": observed["stderr"],
        "cleanup_complete": cleanup_complete,
    }


def _classify(
    process: dict[str, object], verifier_id: str
) -> tuple[str, str, dict[str, object] | None]:
    if process["state"] != "COMPLETED":
        return "APPARATUS_ERROR", str(process["state"]), None
    if process["stderr"]:
        return "APPARATUS_ERROR", "VERIFIER_STDERR_NONEMPTY", None
    try:
        parsed = _strict_object(
            process["stdout"], code="VERIFIER_OUTPUT_INVALID"  # type: ignore[arg-type]
        )
    except KitError as error:
        return "APPARATUS_ERROR", error.code, None
    if (
        parsed.get("schema") != VERIFIER_RESULT_SCHEMA
        or parsed.get("verifier_id") != verifier_id
        or type(parsed.get("accepted")) is not bool
    ):
        return "APPARATUS_ERROR", "VERIFIER_OUTPUT_INVALID", None
    if parsed["accepted"] is True and process["exit_code"] == 0:
        return "PASS", "ACCEPTED", parsed
    if parsed["accepted"] is False and process["exit_code"] == 1:
        return "REJECTED", "CANDIDATE_REJECTED", parsed
    return "APPARATUS_ERROR", "VERIFIER_RESULT_EXIT_MISMATCH", None


def _project_output(parsed: dict[str, object] | None) -> tuple[object, str, str | None]:
    if parsed is None:
        return None, "NONE", None
    encoded = _canonical_json(parsed)
    digest = hashlib.sha256(encoded).hexdigest()
    if len(encoded) <= MAXIMUM_INLINE_VERIFIER_RESULT_BYTES:
        return parsed, "INLINE_COMPLETE", digest
    reason_code = parsed.get("reason_code")
    return (
        {
            "schema": parsed.get("schema"),
            "verifier_id": parsed.get("verifier_id"),
            "accepted": parsed.get("accepted"),
            "reason_code": (
                reason_code
                if type(reason_code) is str
                and len(reason_code.encode("utf-8")) <= 1_024
                else None
            ),
        },
        "CORE_FIELDS_ONLY",
        digest,
    )


def _evidence_directory(workspace: Path) -> Path:
    evidence = workspace / EVIDENCE_DIRECTORY_NAME
    try:
        metadata = evidence.lstat()
    except OSError as error:
        raise KitError("KIT_EVIDENCE_DIRECTORY_UNAVAILABLE", str(evidence)) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise KitError("KIT_EVIDENCE_DIRECTORY_UNSAFE", str(evidence))
    for name in ("verdicts", "receipts"):
        (evidence / name).mkdir(mode=0o700, exist_ok=True)
    return evidence


def _publish_invocation(
    evidence: Path,
    *,
    verdict: dict[str, object],
    receipt: dict[str, object],
) -> tuple[int, Path, Path]:
    """Claim the next ordinal exclusively, then publish both documents."""

    verdict_root = evidence / "verdicts"
    receipt_root = evidence / "receipts"
    ordinal = 0
    try:
        published = sum(
            1
            for entry in os.scandir(receipt_root)
            if entry.is_file(follow_symlinks=False)
        )
    except OSError as error:
        raise KitError("KIT_EVIDENCE_WRITE_FAILED", str(receipt_root)) from error
    # Probing upward from the observed count keeps ordinal claiming linear even
    # after many invocations; exclusivity still comes from ``O_EXCL`` alone.
    for candidate in range(max(1, published + 1), MAXIMUM_INVOCATIONS + 1):
        name = f"{candidate:06d}.json"
        try:
            descriptor = os.open(
                receipt_root / name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except FileExistsError:
            continue
        except OSError as error:
            raise KitError("KIT_EVIDENCE_WRITE_FAILED", name) from error
        ordinal = candidate
        os.close(descriptor)
        break
    if ordinal == 0:
        raise KitError("KIT_EVIDENCE_LIMIT_REACHED", str(MAXIMUM_INVOCATIONS))
    name = f"{ordinal:06d}.json"
    verdict_path = verdict_root / name
    receipt_path = receipt_root / name
    verdict["invocation_ordinal"] = ordinal
    verdict_raw = _canonical_json(verdict) + b"\n"
    _write_exact(verdict_path, verdict_raw, mode=0o600)
    receipt["invocation_ordinal"] = ordinal
    receipt["verdict_sha256"] = hashlib.sha256(verdict_raw).hexdigest()
    receipt["verdict_path"] = f"verdicts/{name}"
    receipt_raw = _canonical_json(receipt) + b"\n"
    descriptor = os.open(
        receipt_path, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        view = memoryview(receipt_raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise KitError("KIT_EVIDENCE_WRITE_FAILED", name)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return ordinal, verdict_path, receipt_path


def _verify(
    *,
    kit_root: Path,
    workspace: Path,
    bindings: dict[str, object],
    target: dict[str, object],
    candidate_argument: str,
    manifest_sha256: str,
    content_sha256: str,
) -> tuple[dict[str, object], dict[str, object]]:
    started_at = _now()
    verifier_id = str(target.get("verifier_id"))
    interpreter = bindings.get("interpreter")
    execution = bindings.get("execution")
    if type(interpreter) is not dict or type(execution) is not dict:
        raise KitError("KIT_BINDINGS_INVALID", "execution identity")
    bootstrap = execution.get("offline_bootstrap")
    if (
        type(bootstrap) is not str
        or hashlib.sha256(bootstrap.encode("utf-8")).hexdigest()
        != execution.get("offline_bootstrap_sha256")
    ):
        raise KitError("KIT_BOOTSTRAP_PIN_MISMATCH")
    timeout_seconds = target.get("timeout_seconds")
    output_cap = target.get("maximum_output_bytes")
    command = target.get("command")
    working_directory = target.get("working_directory")
    if (
        type(timeout_seconds) is not int
        or timeout_seconds < 1
        or type(output_cap) is not int
        or output_cap < 1
        or type(command) is not list
        or not command
        or type(working_directory) is not str
    ):
        raise KitError("KIT_BINDINGS_INVALID", "verifier command")

    candidate: dict[str, object] = {
        "workspace_relative_path": None,
        "sha256": None,
        "bytes": None,
        "supplied": candidate_argument[:1_024],
    }
    status = "APPARATUS_ERROR"
    diagnostic = "KIT_NOT_EXECUTED"
    parsed: dict[str, object] | None = None
    process: dict[str, object] = {
        "state": "NOT_EXECUTED",
        "exit_code": None,
        "signal": None,
        "elapsed_ms": 0,
        "stdout": b"",
        "stderr": b"",
        "stdout_observed_bytes": 0,
        "stderr_observed_bytes": 0,
        "cleanup_complete": True,
    }
    source_closure_sha256: str | None = None
    interpreter_matches: bool | None = None
    try:
        relative = _workspace_relative(workspace, candidate_argument)
        capture = _capture_candidate(workspace, relative)
        candidate.update(capture)
        digest, _count = _hash_absolute_regular(
            Path(str(interpreter.get("path"))),
            maximum_bytes=MAXIMUM_INTERPRETER_BYTES,
            code="KIT_INTERPRETER_UNAVAILABLE",
        )
        interpreter_matches = digest == interpreter.get("sha256")
        if not interpreter_matches:
            raise KitError("KIT_INTERPRETER_DRIFT")
        evidence = workspace / EVIDENCE_DIRECTORY_NAME
        with tempfile.TemporaryDirectory(
            prefix=".pmw-kit-exec-", dir=evidence
        ) as temporary_name:
            temporary = Path(temporary_name)
            source = temporary / "source"
            source.mkdir(mode=0o700)
            source_closure_sha256 = _stage_source(kit_root, target, source)
            working = (
                source
                if working_directory == "."
                else source.joinpath(*PurePosixPath(working_directory).parts)
            )
            working.mkdir(mode=0o700, parents=True, exist_ok=True)
            home = temporary / "home"
            temporary_tmp = temporary / "tmp"
            home.mkdir(mode=0o700)
            temporary_tmp.mkdir(mode=0o700)
            entrypoint = source.joinpath(*PurePosixPath(str(command[0])).parts)
            arguments = [
                str(workspace / relative) if item == "{candidate_path}" else str(item)
                for item in command[1:]
            ]
            process = _run_bounded(
                command=[
                    str(interpreter.get("path")),
                    "-I",
                    "-c",
                    bootstrap,
                    str(entrypoint),
                    *arguments,
                ],
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
                    "TMPDIR": str(temporary_tmp),
                },
                timeout_seconds=timeout_seconds,
                output_cap=output_cap,
            )
        status, diagnostic, parsed = _classify(process, verifier_id)
    except KitError as error:
        status = "APPARATUS_ERROR"
        diagnostic = error.code
    except OSError as error:
        status = "APPARATUS_ERROR"
        diagnostic = f"KIT_LOCAL_IO_FAILED:{type(error).__name__}"[:128]

    projected, projection, output_digest = _project_output(parsed)
    verdict: dict[str, object] = {
        "schema": VERDICT_SCHEMA,
        "authority": IN_SESSION_AUTHORITY,
        "settlement_authority": SETTLEMENT_AUTHORITY,
        "status": status,
        "diagnostic_code": diagnostic,
        "target": {
            "target_id": target.get("target_id"),
            "target_sha256": target.get("target_sha256"),
            "verification_mode": target.get("verification_mode"),
        },
        "candidate": dict(candidate),
        "verifier": {
            "verifier_id": verifier_id,
            "protocol": target.get("protocol"),
            "registry_sha256": target.get("registry_sha256"),
            "manifest_path": target.get("manifest_path"),
            "manifest_sha256": target.get("manifest_sha256"),
            "source_closure_sha256": source_closure_sha256,
        },
        "verifier_output": projected,
        "verifier_output_binding": {
            "canonical_value_sha256": output_digest,
            "projection": projection,
        },
        "claim_ceiling": CLAIM_CEILING,
        "host_verification_command": (
            "pmw-research verifier run --cohort <cohort> --session-id <session> "
            f"--target-id {target.get('target_id')} --candidate "
            f"{candidate.get('workspace_relative_path')}"
        ),
    }
    receipt: dict[str, object] = {
        "schema": INVOCATION_SCHEMA,
        "authority": IN_SESSION_AUTHORITY,
        "settlement_authority": SETTLEMENT_AUTHORITY,
        "kit_manifest_sha256": manifest_sha256,
        "kit_content_sha256": content_sha256,
        "started_at": started_at,
        "finished_at": _now(),
        "target_id": target.get("target_id"),
        "verifier_id": verifier_id,
        "status": status,
        "diagnostic_code": diagnostic,
        "candidate": dict(candidate),
        "source_closure_sha256": source_closure_sha256,
        "execution": {
            "interpreter_sha256": interpreter.get("sha256"),
            "interpreter_sha256_matches": interpreter_matches,
            "offline_bootstrap_sha256": execution.get("offline_bootstrap_sha256"),
            "python_isolated": True,
            "credential_inheritance": False,
            "top_level_python_socket_audit": True,
            "os_network_isolation": False,
            "network_boundary": execution.get("network_boundary"),
            "timeout_seconds": timeout_seconds,
            "maximum_output_bytes": output_cap,
            "process_state": process["state"],
            "exit_code": process["exit_code"],
            "signal": process["signal"],
            "elapsed_ms": process["elapsed_ms"],
            "cleanup_complete": process["cleanup_complete"],
            "stdout": {
                "observed_bytes": process["stdout_observed_bytes"],
                "retained_bytes": len(process["stdout"]),  # type: ignore[arg-type]
                "sha256": hashlib.sha256(process["stdout"]).hexdigest(),  # type: ignore[arg-type]
            },
            "stderr": {
                "observed_bytes": process["stderr_observed_bytes"],
                "retained_bytes": len(process["stderr"]),  # type: ignore[arg-type]
                "sha256": hashlib.sha256(process["stderr"]).hexdigest(),  # type: ignore[arg-type]
            },
        },
    }
    return verdict, receipt


def _emit(value: object, *, stream: object) -> None:
    print(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True),
        file=stream,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="amf-verify",
        description=(
            "Run the pinned AMF verifier locally against one workspace "
            "candidate. Verdicts are advisory in-session evidence; the host's "
            "post-settlement re-execution remains the sole authority."
        ),
    )
    parser.add_argument("candidate", nargs="?", help="workspace candidate path")
    parser.add_argument("--target", help="target ID from the frozen briefing")
    parser.add_argument(
        "--list-targets",
        action="store_true",
        help="print the frozen target/verifier bindings and exit",
    )
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as request:
        # ``--help`` is a successful request; a malformed argument is not.
        return EXIT_PASS if request.code in (0, None) else EXIT_USAGE

    kit_root = Path(__file__).resolve().parent.parent
    workspace = kit_root.parent
    try:
        _manifest, bindings, manifest_sha256, content_sha256 = _load_kit(kit_root)
        if arguments.list_targets:
            _emit(
                {
                    "schema": "PMW_IN_SESSION_VERIFIER_KIT_TARGETS_1",
                    "authority": IN_SESSION_AUTHORITY,
                    "settlement_authority": SETTLEMENT_AUTHORITY,
                    "kit_content_sha256": content_sha256,
                    "targets": [
                        {
                            "target_id": row.get("target_id"),
                            "verification_mode": row.get("verification_mode"),
                            "verifier_id": row.get("verifier_id"),
                        }
                        for row in bindings["targets"]  # type: ignore[index]
                        if type(row) is dict
                    ],
                },
                stream=sys.stdout,
            )
            return EXIT_PASS
        if arguments.candidate is None:
            raise KitError("CANDIDATE_PATH_REQUIRED", "usage: amf-verify CANDIDATE")
        target = _select_target(bindings, arguments.target)
        _evidence_directory(workspace)
    except KitError as error:
        _emit(
            {
                "schema": "PMW_IN_SESSION_VERIFIER_KIT_ERROR_1",
                "authority": IN_SESSION_AUTHORITY,
                "code": error.code,
                "detail": error.detail,
            },
            stream=sys.stderr,
        )
        return EXIT_USAGE

    verdict, receipt = _verify(
        kit_root=kit_root,
        workspace=workspace,
        bindings=bindings,
        target=target,
        candidate_argument=arguments.candidate,
        manifest_sha256=manifest_sha256,
        content_sha256=content_sha256,
    )
    try:
        ordinal, verdict_path, receipt_path = _publish_invocation(
            workspace / EVIDENCE_DIRECTORY_NAME, verdict=verdict, receipt=receipt
        )
    except (KitError, OSError) as error:
        _emit(
            {
                "schema": "PMW_IN_SESSION_VERIFIER_KIT_ERROR_1",
                "authority": IN_SESSION_AUTHORITY,
                "code": getattr(error, "code", "KIT_EVIDENCE_WRITE_FAILED"),
                "detail": str(error)[:512],
                "verdict": verdict,
            },
            stream=sys.stderr,
        )
        return EXIT_APPARATUS_ERROR
    _emit(
        {
            **verdict,
            "invocation_ordinal": ordinal,
            "verdict_path": str(verdict_path),
            "invocation_receipt_path": str(receipt_path),
        },
        stream=sys.stdout,
    )
    if verdict["status"] == "PASS":
        return EXIT_PASS
    if verdict["status"] == "REJECTED":
        return EXIT_REJECTED
    return EXIT_APPARATUS_ERROR


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
