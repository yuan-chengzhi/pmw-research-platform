"""A read-only, pinned in-session AMF verifier kit for research workspaces.

The host verifier (:mod:`pmw_platform.verifier`) stays the sole authority: it
runs after settlement, captures the exact candidate bytes into the artifact
CAS, and publishes an immutable receipt.  Nothing here changes that path.

What this module adds is the missing *in-vivo* half.  Across the twelve live
lives of the previous stack an agent invoked a verifier zero times, partly
because no verifier existed inside a session and partly because nothing told
the session that one could exist.  A closure-endpoint experiment needs a
verification path that is actually exercised during research, so the host
materializes the same pinned verifier bytes into the session workspace behind
one small wrapper CLI::

    <workspace>/.pmw-verifier-kit/bin/amf-verify [--target ID] CANDIDATE

Every invocation writes a machine-readable verdict and an invocation receipt
under ``<workspace>/.pmw-verifier-evidence/``.  Those verdicts are explicitly
``ADVISORY_IN_SESSION_VERIFICATION``.

Three deliberate boundaries:

* the kit carries no credential material and no host data-root path — only
  platform-shipped bytes, pinned AMF verifier bytes, public pin digests and
  the resolved interpreter path;
* the session-local evidence directory lives inside the workspace, not inside
  the host-owned ``evidence/`` tree, so the resource guard keeps accounting for
  every byte an agent writes and host evidence stays host-written;
* the workspace belongs to the session, so read-only mode bits are hygiene,
  not containment.  The runner therefore re-checks every pinned digest at
  invocation time, and the host counts the ledger as *observed* evidence, never
  as a tamper-proof measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Mapping, NoReturn, Sequence

from .source_materializer import SourceMaterializer, SourceMaterializerError
from .verifier import (
    NETWORK_POLICY,
    VERIFIER_PROTOCOL,
    AmfVerifierService,
    InterpreterIdentity,
    TargetVerifierBinding,
    VerifierServiceError,
    # The in-session runner must execute *byte-identical* bootstrap code, so
    # the shared offline bootstrap is taken from the authoritative module
    # instead of being restated here where the two copies could drift.
    _BOOTSTRAP_SHA256,
    _OFFLINE_BOOTSTRAP,
)
from .world.records import canonical_json


VERIFIER_KIT_SCHEMA = "PMW_IN_SESSION_VERIFIER_KIT_1"
VERIFIER_KIT_BINDINGS_SCHEMA = "PMW_IN_SESSION_VERIFIER_KIT_BINDINGS_1"
VERIFIER_KIT_LAUNCH_SCHEMA = "PMW_IN_SESSION_VERIFIER_KIT_LAUNCH_1"
VERIFIER_KIT_SESSION_EVIDENCE_SCHEMA = (
    "PMW_IN_SESSION_VERIFIER_KIT_SESSION_EVIDENCE_1"
)
VERIFIER_KIT_INVOCATION_SCHEMA = "PMW_IN_SESSION_VERIFIER_INVOCATION_1"
VERIFIER_KIT_VERDICT_SCHEMA = "PMW_IN_SESSION_VERIFIER_VERDICT_1"

IN_SESSION_VERIFICATION_AUTHORITY = "ADVISORY_IN_SESSION_VERIFICATION"
SETTLEMENT_VERIFICATION_AUTHORITY = "HOST_REEXECUTED_PINNED_AMF_VERIFIER"
LEDGER_COUNTING_AUTHORITY = (
    "HOST_OBSERVED_SESSION_LOCAL_ADVISORY_LEDGER_NOT_TAMPER_PROOF"
)

KIT_DIRECTORY_NAME = ".pmw-verifier-kit"
KIT_EVIDENCE_DIRECTORY_NAME = ".pmw-verifier-evidence"
KIT_WRAPPER_RELATIVE_PATH = "bin/amf-verify"
KIT_ENTRYPOINT = f"{KIT_DIRECTORY_NAME}/{KIT_WRAPPER_RELATIVE_PATH}"
KIT_RUNNER_RELATIVE_PATH = "lib/amf_verify.py"
KIT_BINDINGS_RELATIVE_PATH = "lib/bindings.json"
KIT_MANIFEST_RELATIVE_PATH = "manifest.json"

VERDICT_STATUSES = ("PASS", "REJECTED", "APPARATUS_ERROR")
MAXIMUM_KIT_FILES = 4_096
MAXIMUM_KIT_BYTES = 64 * 1024 * 1024
# The launch block must stay a small, fixed-size identity for any portfolio,
# and the announcement must stay readable; ``--list-targets`` remains the
# complete, unbounded enumeration inside the session.
MAXIMUM_ANNOUNCED_TARGET_IDS = 64
MAXIMUM_LEDGER_ENTRIES = 100_000
MAXIMUM_LEDGER_ENTRY_BYTES = 1_048_576

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Duplicated from the runtime contracts on purpose: this module must stay
# importable without the runtime package, and the repository already keeps one
# copy of this pattern per trust boundary.
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:access[_-]?token|refresh[_-]?token|bearer[_-]?token|"
    r"api[_-]?key|client[_-]?secret|password|credential[_-]?value|"
    r"private[_-]?key|session[_-]?secret)(?:$|[_-])",
    re.IGNORECASE,
)


class VerifierKitError(ValueError):
    """The in-session verifier kit could not be built or materialized."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _fail(code: str, detail: str = "") -> NoReturn:
    raise VerifierKitError(code, detail)


def _reject_sensitive_keys(value: object) -> None:
    stack = [value]
    while stack:
        selected = stack.pop()
        if type(selected) is dict:
            for key, child in selected.items():
                if type(key) is not str:
                    _fail("VERIFIER_KIT_BINDINGS_INVALID", "non-text key")
                if _SENSITIVE_KEY.search(key):
                    _fail("VERIFIER_KIT_CREDENTIAL_SUSPECTED", key[:128])
                stack.append(child)
        elif type(selected) is list:
            stack.extend(selected)


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _relative_kit_path(value: str) -> str:
    pure = PurePosixPath(value)
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        _fail("VERIFIER_KIT_PATH_INVALID", value[:256])
    return value


@dataclass(frozen=True, slots=True)
class KitFile:
    """One exact byte sequence published into the session workspace."""

    path: str
    content: bytes
    mode: int

    def row(self) -> dict[str, object]:
        return {
            "path": self.path,
            "bytes": len(self.content),
            "sha256": hashlib.sha256(self.content).hexdigest(),
            "mode": f"{self.mode:04o}",
        }


@dataclass(frozen=True, slots=True)
class VerifierKit:
    """An immutable, path-independent kit shared by every cohort session."""

    files: tuple[KitFile, ...]
    content_sha256: str
    manifest_sha256: str
    sha256: str
    target_ids: tuple[str, ...]
    verifier_ids: tuple[str, ...]
    source_commit: str
    source_tree_sha256: str
    registry_sha256: str
    interpreter_sha256: str

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(len(item.content) for item in self.files)

    @property
    def target_ids_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json(sorted(self.target_ids))
        ).hexdigest()

    def launch_value(self) -> dict[str, object]:
        """Return the bounded public kit identity frozen into ``launch.json``."""

        return {
            "schema": VERIFIER_KIT_LAUNCH_SCHEMA,
            "mode": "MATERIALIZED",
            "authority": IN_SESSION_VERIFICATION_AUTHORITY,
            "settlement_authority": SETTLEMENT_VERIFICATION_AUTHORITY,
            "kit_directory": KIT_DIRECTORY_NAME,
            "evidence_directory": KIT_EVIDENCE_DIRECTORY_NAME,
            "entrypoint": KIT_ENTRYPOINT,
            "kit_sha256": self.sha256,
            "content_sha256": self.content_sha256,
            "manifest_sha256": self.manifest_sha256,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "source_commit": self.source_commit,
            "source_tree_sha256": self.source_tree_sha256,
            "registry_sha256": self.registry_sha256,
            "interpreter_sha256": self.interpreter_sha256,
            "target_count": len(self.target_ids),
            "target_ids_sha256": self.target_ids_sha256,
            "credential_material": False,
        }

    def materialize(self, workspace: str | os.PathLike[str]) -> Path:
        """Publish the kit read-only and create the session evidence root."""

        root = Path(workspace)
        if not root.is_absolute():
            _fail("VERIFIER_KIT_WORKSPACE_INVALID", "workspace must be absolute")
        try:
            metadata = root.lstat()
        except OSError as error:
            raise VerifierKitError(
                "VERIFIER_KIT_WORKSPACE_INVALID", str(root)
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            _fail("VERIFIER_KIT_WORKSPACE_INVALID", str(root))
        kit_root = root / KIT_DIRECTORY_NAME
        evidence_root = root / KIT_EVIDENCE_DIRECTORY_NAME
        try:
            os.mkdir(kit_root, 0o700)
            os.mkdir(evidence_root, 0o700)
            for item in self.files:
                destination = kit_root.joinpath(*PurePosixPath(item.path).parts)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _write_exact(destination, item.content, mode=0o600)
                os.chmod(destination, item.mode)
            directories = [kit_root]
            for current, names, _files in os.walk(
                kit_root, topdown=True, followlinks=False
            ):
                directories.extend(Path(current) / name for name in names)
            for directory in reversed(directories):
                _fsync_directory(directory)
                os.chmod(directory, 0o555)
            _fsync_directory(root)
        except FileExistsError as error:
            raise VerifierKitError(
                "VERIFIER_KIT_PATH_OCCUPIED",
                f"{KIT_DIRECTORY_NAME} or {KIT_EVIDENCE_DIRECTORY_NAME} in {root}",
            ) from error
        except OSError as error:
            raise VerifierKitError(
                "VERIFIER_KIT_MATERIALIZATION_FAILED", str(kit_root)
            ) from error
        return kit_root

    def briefing_announcement(self) -> dict[str, object]:
        """Announce the capability without recommending a research route.

        The wording deliberately states only what exists, how to call it, where
        its evidence lands and what it does *not* decide.  It gives no
        instruction to verify, no ordering and no success criterion, because the
        experiment measures whether sessions choose a verification route, not
        whether they can follow one the host prescribed.
        """

        return {
            "schema": VERIFIER_KIT_SCHEMA,
            "available": True,
            "authority": IN_SESSION_VERIFICATION_AUTHORITY,
            "settlement_authority": SETTLEMENT_VERIFICATION_AUTHORITY,
            "invocation": {
                "command": f"./{KIT_ENTRYPOINT} [--target TARGET_ID] CANDIDATE_PATH",
                "list_targets": f"./{KIT_ENTRYPOINT} --list-targets",
                "working_directory": "the session workspace root",
                "candidate_path": "any regular file inside the session workspace",
                "exit_codes": {
                    "0": "PASS",
                    "1": "REJECTED",
                    "2": "APPARATUS_ERROR",
                    "64": "invalid invocation; no verdict is written",
                },
            },
            "writes": {
                "verdict": f"{KIT_EVIDENCE_DIRECTORY_NAME}/verdicts/<ordinal>.json",
                "invocation_receipt": (
                    f"{KIT_EVIDENCE_DIRECTORY_NAME}/receipts/<ordinal>.json"
                ),
                "counted_in": "this session's host receipt",
            },
            "target_count": len(self.target_ids),
            "target_ids": list(self.target_ids[:MAXIMUM_ANNOUNCED_TARGET_IDS]),
            "target_ids_complete": (
                len(self.target_ids) <= MAXIMUM_ANNOUNCED_TARGET_IDS
            ),
            "kit_sha256": self.sha256,
            "statements": [
                "The kit executes the same content-pinned AMF verifier bytes "
                "the host re-executes after settlement.",
                "An in-session verdict is advisory evidence only: it is not an "
                "admission, not a novelty claim, and not a solved open problem.",
                "Running the kit is neither required nor recommended by the "
                "host; this record announces a capability and no route.",
                "Not running the kit is a permitted outcome and is recorded as "
                "an invocation count of zero.",
            ],
        }


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exact(path: Path, raw: bytes, *, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("VERIFIER_KIT_MATERIALIZATION_FAILED", str(path))
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _runner_bytes() -> bytes:
    """Read the exact runner bytes shipped with the installed platform."""

    path = Path(__file__).resolve().parent / "verifier_kit_runner.py"
    try:
        if path.is_symlink() or not path.is_file():
            _fail("VERIFIER_KIT_RUNNER_UNAVAILABLE", str(path))
        raw = path.read_bytes()
    except VerifierKitError:
        raise
    except OSError as error:
        raise VerifierKitError(
            "VERIFIER_KIT_RUNNER_UNAVAILABLE", str(path)
        ) from error
    if not 1 <= len(raw) <= MAXIMUM_KIT_BYTES:
        _fail("VERIFIER_KIT_RUNNER_UNAVAILABLE", "size")
    return raw


def _wrapper_bytes(interpreter: Path) -> bytes:
    quoted = _shell_single_quote(str(interpreter))
    return (
        "#!/bin/sh\n"
        "# PMW in-session AMF verifier kit wrapper.\n"
        "#\n"
        "# usage: amf-verify [--target TARGET_ID] CANDIDATE\n"
        "#        amf-verify --list-targets\n"
        "#\n"
        "# Executes the content-pinned AMF verifier locally against one\n"
        "# workspace candidate and writes a verdict plus an invocation receipt\n"
        f"# under <workspace>/{KIT_EVIDENCE_DIRECTORY_NAME}/.\n"
        f"# Verdicts are {IN_SESSION_VERIFICATION_AUTHORITY}; only the host's\n"
        "# post-settlement re-execution of the same pinned verifier is\n"
        "# authoritative.\n"
        "set -eu\n"
        "kit_root=$(CDPATH='' cd -- \"$(dirname -- \"$0\")/..\" && pwd -P)\n"
        f"exec {quoted} -I \"$kit_root/{KIT_RUNNER_RELATIVE_PATH}\" \"$@\"\n"
    ).encode("utf-8", errors="strict")


def _bindings_value(
    *,
    materialized_name: str,
    repository: str,
    commit: str,
    git_tree: str,
    tree_sha256: str,
    manifest_sha256: str,
    registry_sha256: str,
    interpreter: InterpreterIdentity,
    targets: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema": VERIFIER_KIT_BINDINGS_SCHEMA,
        "authority": IN_SESSION_VERIFICATION_AUTHORITY,
        "settlement_authority": SETTLEMENT_VERIFICATION_AUTHORITY,
        "source": {
            "name": materialized_name,
            "repository": repository,
            "commit": commit,
            "git_tree": git_tree,
            "materializer_tree_sha256": tree_sha256,
            "materializer_manifest_sha256": manifest_sha256,
            "registry_path": "data/verifiers.json",
            "registry_sha256": registry_sha256,
        },
        "interpreter": interpreter.as_dict(),
        "execution": {
            "offline_bootstrap": _OFFLINE_BOOTSTRAP,
            "offline_bootstrap_sha256": _BOOTSTRAP_SHA256,
            "python_isolated": True,
            "credential_inheritance": False,
            "os_network_isolation": False,
            "network_boundary": NETWORK_POLICY,
        },
        "targets": targets,
    }


def build_verifier_kit(
    *,
    source_materializer: SourceMaterializer,
    target_bindings: Sequence[TargetVerifierBinding],
    python_executable: str | os.PathLike[str],
    source_name: str = "agent-math-frontier",
) -> VerifierKit:
    """Build the pinned kit from the audited locked source and one briefing.

    The verifier source, registry and manifest pins are loaded through the
    authoritative :class:`AmfVerifierService` loader, so the bytes shipped into
    a workspace are exactly the bytes settlement will re-execute.  No verifier
    is executed here and no model or network request is made.
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
    try:
        interpreter_path = Path(python_executable).expanduser().resolve(strict=True)
    except OSError as error:
        raise VerifierKitError("VERIFIER_KIT_INTERPRETER_INVALID") from error

    # ``audit_portfolio`` uses the same read-only inspector construction; the
    # kit additionally needs the loaded definitions and interpreter identity.
    inspector = object.__new__(AmfVerifierService)
    inspector.source_materializer = source_materializer
    inspector.source_name = source_name
    inspector.python_executable = interpreter_path
    try:
        materialized = source_materializer.audit(source_name)
        snapshot = inspector._load_source_snapshot()
        interpreter = inspector._load_interpreter_identity()
        AmfVerifierService._validate_bindings(snapshot, bindings)
    except (SourceMaterializerError, VerifierServiceError) as error:
        raise VerifierKitError(error.code, error.detail) from error

    files: list[KitFile] = []
    seen_source: dict[str, str] = {}
    targets: list[dict[str, object]] = []
    for binding in sorted(bindings, key=lambda item: item.target_id):
        definition = snapshot.definitions[binding.verifier_id]
        artifacts: list[dict[str, object]] = []
        for path, raw, digest in definition.source_artifacts:
            kit_path = _relative_kit_path(f"source/{binding.verifier_id}/{path}")
            previous = seen_source.get(kit_path)
            if previous is None:
                seen_source[kit_path] = digest
                files.append(KitFile(path=kit_path, content=raw, mode=0o444))
            elif previous != digest:
                _fail("VERIFIER_KIT_SOURCE_CONFLICT", kit_path)
            artifacts.append(
                {"path": path, "bytes": len(raw), "sha256": digest}
            )
        targets.append(
            {
                "target_id": binding.target_id,
                "target_sha256": binding.target_sha256,
                "verification_mode": binding.verification_mode,
                "verifier_id": definition.verifier_id,
                "protocol": VERIFIER_PROTOCOL,
                "registry_sha256": snapshot.registry_sha256,
                "manifest_path": definition.registry_manifest_path,
                "manifest_sha256": definition.manifest_sha256,
                "manifest_bytes": definition.manifest_bytes,
                "source_closure_sha256": definition.source_closure_sha256,
                "command": list(definition.command),
                "working_directory": definition.working_directory,
                "timeout_seconds": definition.timeout_seconds,
                "maximum_output_bytes": definition.maximum_output_bytes,
                "source_artifacts": artifacts,
            }
        )

    bindings_value = _bindings_value(
        materialized_name=materialized.name,
        repository=materialized.repository,
        commit=materialized.commit,
        git_tree=materialized.git_tree,
        tree_sha256=materialized.tree_sha256,
        manifest_sha256=materialized.manifest_sha256,
        registry_sha256=snapshot.registry_sha256,
        interpreter=interpreter,
        targets=targets,
    )
    _reject_sensitive_keys(bindings_value)
    files.append(
        KitFile(
            path=KIT_BINDINGS_RELATIVE_PATH,
            content=canonical_json(bindings_value) + b"\n",
            mode=0o444,
        )
    )
    files.append(
        KitFile(
            path=KIT_RUNNER_RELATIVE_PATH, content=_runner_bytes(), mode=0o444
        )
    )
    files.append(
        KitFile(
            path=KIT_WRAPPER_RELATIVE_PATH,
            content=_wrapper_bytes(interpreter_path),
            mode=0o555,
        )
    )
    files.sort(key=lambda item: item.path.encode("utf-8"))
    if not 1 <= len(files) <= MAXIMUM_KIT_FILES:
        _fail("VERIFIER_KIT_TOO_LARGE", "file count")
    content_rows = [item.row() for item in files]
    content_sha256 = hashlib.sha256(canonical_json(content_rows)).hexdigest()
    manifest_value: dict[str, object] = {
        "schema": VERIFIER_KIT_SCHEMA,
        "authority": IN_SESSION_VERIFICATION_AUTHORITY,
        "settlement_authority": SETTLEMENT_VERIFICATION_AUTHORITY,
        "entrypoint": KIT_WRAPPER_RELATIVE_PATH,
        "runner": KIT_RUNNER_RELATIVE_PATH,
        "bindings": KIT_BINDINGS_RELATIVE_PATH,
        "evidence_directory": f"../{KIT_EVIDENCE_DIRECTORY_NAME}",
        "content_provenance": [
            "PLATFORM_SHIPPED_RUNNER_AND_WRAPPER",
            "PINNED_AMF_VERIFIER_SOURCE_BYTES",
            "PUBLIC_PIN_DIGESTS_AND_RESOLVED_INTERPRETER_PATH",
        ],
        "credential_material": False,
        "content_sha256": content_sha256,
        "file_count": len(files),
        "total_bytes": sum(len(item.content) for item in files),
        "files": content_rows,
    }
    manifest = KitFile(
        path=KIT_MANIFEST_RELATIVE_PATH,
        content=canonical_json(manifest_value) + b"\n",
        mode=0o444,
    )
    complete = tuple(
        sorted([*files, manifest], key=lambda item: item.path.encode("utf-8"))
    )
    total_bytes = sum(len(item.content) for item in complete)
    if not 1 <= total_bytes <= MAXIMUM_KIT_BYTES:
        _fail("VERIFIER_KIT_TOO_LARGE", str(total_bytes))
    identity = {
        "schema": VERIFIER_KIT_SCHEMA,
        "files": [item.row() for item in complete],
    }
    return VerifierKit(
        files=complete,
        content_sha256=content_sha256,
        manifest_sha256=hashlib.sha256(manifest.content).hexdigest(),
        sha256=hashlib.sha256(canonical_json(identity)).hexdigest(),
        target_ids=tuple(str(row["target_id"]) for row in targets),
        verifier_ids=tuple(
            sorted({str(row["verifier_id"]) for row in targets})
        ),
        source_commit=materialized.commit,
        source_tree_sha256=materialized.tree_sha256,
        registry_sha256=snapshot.registry_sha256,
        interpreter_sha256=interpreter.sha256,
    )


def disabled_verifier_kit_launch_value() -> dict[str, object]:
    """Return the exact launch block used when no kit is materialized."""

    return {
        "schema": VERIFIER_KIT_LAUNCH_SCHEMA,
        "mode": "DISABLED",
        "authority": IN_SESSION_VERIFICATION_AUTHORITY,
        "settlement_authority": SETTLEMENT_VERIFICATION_AUTHORITY,
        "reason": "NO_IN_SESSION_VERIFIER_KIT_MATERIALIZED",
    }


def _session_evidence_value(
    *,
    mode: str,
    ledger: str,
    kit_content_sha256: str | None,
    counts: Mapping[str, int],
    rejected_entries: int,
    truncated: bool,
) -> dict[str, object]:
    return {
        "schema": VERIFIER_KIT_SESSION_EVIDENCE_SCHEMA,
        "mode": mode,
        "authority": IN_SESSION_VERIFICATION_AUTHORITY,
        "settlement_authority": SETTLEMENT_VERIFICATION_AUTHORITY,
        "counting_authority": LEDGER_COUNTING_AUTHORITY,
        "evidence_directory": KIT_EVIDENCE_DIRECTORY_NAME,
        "kit_content_sha256": kit_content_sha256,
        "ledger": ledger,
        "invocation_count": sum(counts[status] for status in VERDICT_STATUSES),
        "verdict_counts": {status: counts[status] for status in VERDICT_STATUSES},
        "rejected_entries": rejected_entries,
        "truncated": truncated,
    }


def disabled_verifier_kit_session_evidence() -> dict[str, object]:
    """Return the receipt block used when a session received no kit."""

    return _session_evidence_value(
        mode="DISABLED",
        ledger="NOT_MATERIALIZED",
        kit_content_sha256=None,
        counts={status: 0 for status in VERDICT_STATUSES},
        rejected_entries=0,
        truncated=False,
    )


def _empty_ledger(kit: "VerifierKit", ledger: str) -> dict[str, object]:
    return _session_evidence_value(
        mode="MATERIALIZED",
        ledger=ledger,
        kit_content_sha256=kit.content_sha256,
        counts={status: 0 for status in VERDICT_STATUSES},
        rejected_entries=0,
        truncated=False,
    )


def unreadable_verifier_kit_session_evidence(kit: "VerifierKit") -> dict[str, object]:
    """Return the receipt block used when the session ledger cannot be read.

    An unreadable advisory ledger is reported as unreadable rather than as
    zero invocations: the two are different observations.
    """

    return _empty_ledger(kit, "UNREADABLE")


def absent_verifier_kit_announcement() -> dict[str, object]:
    """Announce, in the same prompt surface, that no kit was materialized."""

    return {
        "schema": VERIFIER_KIT_SCHEMA,
        "available": False,
        "authority": IN_SESSION_VERIFICATION_AUTHORITY,
        "settlement_authority": SETTLEMENT_VERIFICATION_AUTHORITY,
        "statements": [
            "This launch materialized no in-session verifier kit.",
            "Verification remains a host operation performed after settlement.",
        ],
    }


def _ledger_entry_status(raw: bytes, *, kit_content_sha256: str) -> str | None:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, ValueError, RecursionError):
        return None
    if (
        type(value) is not dict
        or value.get("schema") != VERIFIER_KIT_INVOCATION_SCHEMA
        or value.get("authority") != IN_SESSION_VERIFICATION_AUTHORITY
        or value.get("kit_content_sha256") != kit_content_sha256
        or value.get("status") not in VERDICT_STATUSES
    ):
        return None
    return str(value["status"])


def read_session_verifier_kit_evidence(
    kit: VerifierKit,
    workspace: str | os.PathLike[str],
) -> dict[str, object]:
    """Count the session-local advisory ledger without trusting its contents.

    The workspace belongs to the session, so this is deliberately an
    *observation*: a well-formed invocation receipt bound to this exact kit is
    counted, anything else is reported as a rejected entry, and no verdict here
    is promoted to an authority.
    """

    if not isinstance(kit, VerifierKit):
        raise TypeError("kit must be a VerifierKit")
    counts = {status: 0 for status in VERDICT_STATUSES}
    receipts = Path(workspace) / KIT_EVIDENCE_DIRECTORY_NAME / "receipts"
    try:
        metadata = receipts.lstat()
    except FileNotFoundError:
        return _empty_ledger(kit, "ABSENT")
    except OSError:
        return unreadable_verifier_kit_session_evidence(kit)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return unreadable_verifier_kit_session_evidence(kit)
    rejected = 0
    truncated = False
    try:
        names = sorted(
            entry.name
            for entry in os.scandir(receipts)
            if entry.is_file(follow_symlinks=False)
        )
    except OSError:
        return unreadable_verifier_kit_session_evidence(kit)
    if len(names) > MAXIMUM_LEDGER_ENTRIES:
        truncated = True
        names = names[:MAXIMUM_LEDGER_ENTRIES]
    for name in names:
        try:
            raw = (receipts / name).read_bytes()
        except OSError:
            rejected += 1
            continue
        if len(raw) > MAXIMUM_LEDGER_ENTRY_BYTES:
            rejected += 1
            continue
        status = _ledger_entry_status(raw, kit_content_sha256=kit.content_sha256)
        if status is None:
            rejected += 1
            continue
        counts[status] += 1
    return _session_evidence_value(
        mode="MATERIALIZED",
        ledger="OBSERVED",
        kit_content_sha256=kit.content_sha256,
        counts=counts,
        rejected_entries=rejected,
        truncated=truncated,
    )


__all__ = [
    "IN_SESSION_VERIFICATION_AUTHORITY",
    "KIT_DIRECTORY_NAME",
    "KIT_ENTRYPOINT",
    "KIT_EVIDENCE_DIRECTORY_NAME",
    "LEDGER_COUNTING_AUTHORITY",
    "SETTLEMENT_VERIFICATION_AUTHORITY",
    "VERDICT_STATUSES",
    "VERIFIER_KIT_BINDINGS_SCHEMA",
    "VERIFIER_KIT_INVOCATION_SCHEMA",
    "VERIFIER_KIT_LAUNCH_SCHEMA",
    "VERIFIER_KIT_SCHEMA",
    "VERIFIER_KIT_SESSION_EVIDENCE_SCHEMA",
    "VERIFIER_KIT_VERDICT_SCHEMA",
    "KitFile",
    "VerifierKit",
    "VerifierKitError",
    "absent_verifier_kit_announcement",
    "build_verifier_kit",
    "disabled_verifier_kit_launch_value",
    "disabled_verifier_kit_session_evidence",
    "read_session_verifier_kit_evidence",
    "unreadable_verifier_kit_session_evidence",
]
