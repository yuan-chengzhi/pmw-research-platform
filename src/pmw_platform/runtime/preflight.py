"""Read-only readiness checks for one authenticated runtime launch.

Preflight is deliberately advisory: the real launch repeats every mutable
check while holding :class:`~pmw_platform.runtime.store.RuntimeClaim`.  This
module never creates a runtime directory or claim file, starts a backend, or
uses, refreshes, or serializes credential values.  A backend pin verifier may
bounded-read credential metadata to prove an account boundary (for example,
that the selected provider uses OAuth) but may not return or hash the secret
value.  A caller-provided checker is trusted host code and must honour the
same read-only, offline contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping, Protocol, Sequence

from ..world.records import canonical_json
from .auth import PreparedCohort
from .context import ContextWindowControl, ContextWindowPolicy
from .contracts import BackendIdentity, RuntimeBackend
from .orchestrator import ContributionPublisher, RuntimeLimits
from .publish import PublicationIdentity


PREFLIGHT_REPORT_SCHEMA = "PMW_RUNTIME_PREFLIGHT_REPORT_1"
MAXIMUM_PREFLIGHT_CHECKS = 32
MAXIMUM_PREFLIGHT_CHECK_EVIDENCE_BYTES = 2_048
MAXIMUM_PREFLIGHT_REPORT_BYTES = 131_072
_REPORT_DIGEST_DOMAIN = b"PMW_RUNTIME_PREFLIGHT_REPORT_1\0"
_CHECK_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_CHECK_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_COHORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_HOOK_NAME = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:access[_-]?token|refresh[_-]?token|bearer[_-]?token|"
    r"api[_-]?key|client[_-]?secret|password|credential[_-]?value|"
    r"private[_-]?key|session[_-]?secret)(?:$|[_-])",
    re.IGNORECASE,
)


class PreflightChecker(Protocol):
    """Optional trusted, synchronous, read-only extension check.

    Implementations may inspect pinned tools, verifier sources, or other local
    launch dependencies.  They must not write, spawn work, access the network,
    refresh credentials, or return secret-bearing evidence.  Returning
    ``None`` is equivalent to returning an empty public evidence object;
    raising an exception makes the named check fail.
    """

    @property
    def name(self) -> str: ...

    def verify(
        self,
        *,
        prepared: PreparedCohort,
        backend: RuntimeBackend,
        limits: RuntimeLimits,
        context_policy: ContextWindowPolicy,
        publication_identity: PublicationIdentity,
    ) -> Mapping[str, object] | None: ...


def _public_json_clone(value: object, *, maximum_bytes: int) -> dict[str, object]:
    try:
        encoded = canonical_json(value)
    except Exception as error:
        raise ValueError("preflight evidence is not canonical JSON") from error
    if len(encoded) > maximum_bytes:
        raise ValueError("preflight evidence is too large")
    stack = [value]
    while stack:
        selected = stack.pop()
        if type(selected) is dict:
            for key, child in selected.items():
                if type(key) is not str:
                    raise ValueError("preflight evidence has a non-text key")
                if _SENSITIVE_KEY.search(key):
                    raise ValueError("preflight evidence has a sensitive key")
                stack.append(child)
        elif type(selected) is list:
            stack.extend(selected)
    cloned = json.loads(encoded.decode("utf-8"))
    if type(cloned) is not dict:
        raise ValueError("preflight evidence must be an object")
    return cloned


@dataclass(frozen=True, init=False)
class PreflightCheck:
    """One bounded public PASS/FAIL observation."""

    name: str
    status: str
    code: str
    _evidence_bytes: bytes = field(repr=False)

    def __init__(
        self,
        *,
        name: str,
        status: str,
        code: str,
        evidence: Mapping[str, object] | None = None,
    ) -> None:
        if type(name) is not str or _CHECK_NAME.fullmatch(name) is None:
            raise ValueError("preflight check name is invalid")
        if status not in {"PASS", "FAIL"}:
            raise ValueError("preflight check status must be PASS or FAIL")
        if type(code) is not str or _CHECK_CODE.fullmatch(code) is None:
            raise ValueError("preflight check code is invalid")
        selected = {} if evidence is None else evidence
        cloned = _public_json_clone(
            selected,
            maximum_bytes=MAXIMUM_PREFLIGHT_CHECK_EVIDENCE_BYTES,
        )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "_evidence_bytes", canonical_json(cloned))

    @property
    def evidence(self) -> dict[str, object]:
        value = json.loads(self._evidence_bytes.decode("utf-8"))
        if type(value) is not dict:
            raise AssertionError("preflight check evidence is not an object")
        return value

    def to_value(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "code": self.code,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Canonical, bounded readiness report which is safe to persist later."""

    cohort_id: str
    plan_sha256: str
    backend_sha256: str | None
    publication_sha256: str | None
    context_policy_sha256: str | None
    checks: tuple[PreflightCheck, ...]

    def __post_init__(self) -> None:
        if (
            type(self.cohort_id) is not str
            or _COHORT_ID.fullmatch(self.cohort_id) is None
        ):
            raise ValueError("preflight cohort ID is invalid")
        if (
            type(self.plan_sha256) is not str
            or _SHA256.fullmatch(self.plan_sha256) is None
        ):
            raise ValueError("preflight plan digest is invalid")
        for label in (
            "backend_sha256",
            "publication_sha256",
            "context_policy_sha256",
        ):
            value = getattr(self, label)
            if value is not None and (
                type(value) is not str or _SHA256.fullmatch(value) is None
            ):
                raise ValueError(f"preflight {label} is invalid")
        if (
            not isinstance(self.checks, tuple)
            or not self.checks
            or len(self.checks) > MAXIMUM_PREFLIGHT_CHECKS
        ):
            raise ValueError("preflight report check count is invalid")
        if any(not isinstance(item, PreflightCheck) for item in self.checks):
            raise TypeError("checks must contain PreflightCheck values")
        names = [item.name for item in self.checks]
        if len(set(names)) != len(names):
            raise ValueError("preflight report check names must be unique")
        # Enforce the public envelope at construction, including its digest.
        if len(canonical_json(self.to_value())) > MAXIMUM_PREFLIGHT_REPORT_BYTES:
            raise ValueError("preflight report is too large")

    @property
    def ready(self) -> bool:
        return all(item.status == "PASS" for item in self.checks)

    def _unsigned_value(self) -> dict[str, object]:
        return {
            "schema": PREFLIGHT_REPORT_SCHEMA,
            "cohort_id": self.cohort_id,
            "plan_sha256": self.plan_sha256,
            "backend_sha256": self.backend_sha256,
            "publication_sha256": self.publication_sha256,
            "context_policy_sha256": self.context_policy_sha256,
            "ready": self.ready,
            "checks": [item.to_value() for item in self.checks],
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            _REPORT_DIGEST_DOMAIN + canonical_json(self._unsigned_value())
        ).hexdigest()

    def to_value(self) -> dict[str, object]:
        value = self._unsigned_value()
        value["preflight_sha256"] = self.sha256
        return value

    def to_bytes(self) -> bytes:
        return canonical_json(self.to_value()) + b"\n"


def _error_evidence(error: Exception) -> dict[str, object]:
    value: dict[str, object] = {
        "error_type": f"{type(error).__module__}.{type(error).__qualname__}"[:512]
    }
    code = getattr(error, "code", None)
    if type(code) is str and _SAFE_ERROR_CODE.fullmatch(code) is not None:
        value["error_code"] = code
    return value


def _pass(
    checks: list[PreflightCheck],
    name: str,
    code: str,
    evidence: Mapping[str, object] | None = None,
) -> None:
    checks.append(
        PreflightCheck(name=name, status="PASS", code=code, evidence=evidence)
    )


def _fail(
    checks: list[PreflightCheck],
    name: str,
    code: str,
    evidence: Mapping[str, object] | None = None,
) -> None:
    checks.append(
        PreflightCheck(name=name, status="FAIL", code=code, evidence=evidence)
    )


def _verify_plan(prepared: PreparedCohort) -> tuple[str, dict[str, object]]:
    plan = prepared.plan
    session_ids = tuple(item.session_id for item in plan.sessions)
    retained_briefing_sha256 = hashlib.sha256(prepared.briefing_bytes).hexdigest()
    if (
        not session_ids
        or len(set(session_ids)) != len(session_ids)
        or not 1 <= plan.concurrency <= len(session_ids)
        or plan.cohort_id != prepared.cohort_root.name
        or plan.safety_profile != prepared.profile.name
        or plan.safety_profile_sha256 != prepared.profile.sha256
        or plan.core_lock_sha256 != prepared.core_lock.sha256
        or plan.briefing_sha256 != retained_briefing_sha256
    ):
        raise ValueError("prepared cohort identity drift")
    return plan.sha256, {
        "session_count": len(session_ids),
        "concurrency": plan.concurrency,
        "briefing_sha256": retained_briefing_sha256,
    }


def _verify_backend_identity(backend: RuntimeBackend) -> BackendIdentity:
    identity = backend.identity
    if not isinstance(identity, BackendIdentity):
        raise TypeError("backend.identity must be BackendIdentity")
    rebuilt = BackendIdentity(
        name=identity.name,
        protocol=identity.protocol,
        public_config=identity.public_config,
    )
    if rebuilt.sha256 != identity.sha256:
        raise ValueError("backend identity digest drift")
    return identity


def _verify_backend_runtime(backend: RuntimeBackend) -> None:
    """Run the backend's explicitly public synchronous pin verifier."""

    verifier = getattr(backend, "verify_runtime", None)
    if verifier is None:
        raise TypeError("backend.verify_runtime is required for preflight")
    if not callable(verifier) or inspect.iscoroutinefunction(verifier):
        raise TypeError("backend.verify_runtime must be synchronous and callable")
    result = verifier()
    if inspect.isawaitable(result):
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise TypeError("backend.verify_runtime returned an awaitable")
    if result is not None:
        raise TypeError("backend.verify_runtime must return None")


def _publication_identity(
    publisher: ContributionPublisher | None,
) -> PublicationIdentity:
    if publisher is None:
        return PublicationIdentity.disabled()
    if not callable(publisher):
        raise TypeError("publisher must be callable")
    identity = getattr(publisher, "identity", None)
    if not isinstance(identity, PublicationIdentity):
        raise TypeError("publisher.identity must be PublicationIdentity")
    rebuilt = PublicationIdentity(
        mode=identity.mode,
        protocol=identity.protocol,
        public_config=identity.public_config,
    )
    if rebuilt.sha256 != identity.sha256:
        raise ValueError("publication identity digest drift")
    return identity


def _claim_path_status(cohort_root: Path) -> tuple[str, dict[str, object]]:
    """Observe claim availability without creating or rewriting its inode."""

    path = cohort_root / ".runtime.lock"
    try:
        named = os.lstat(path)
    except FileNotFoundError:
        if not os.access(cohort_root, os.W_OK | os.X_OK):
            raise PermissionError("cohort root cannot create a runtime claim")
        return "RUNTIME_CLAIM_PATH_ABSENT", {
            "claim_file_present": False,
            "launch_rechecks_atomically": True,
        }
    if stat.S_ISLNK(named.st_mode) or not stat.S_ISREG(named.st_mode):
        raise ValueError("runtime claim path is not a regular file")
    if not os.access(path, os.R_OK | os.W_OK):
        raise PermissionError("runtime claim is not readable and writable")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    locked = False
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != named.st_dev
            or opened.st_ino != named.st_ino
        ):
            raise ValueError("runtime claim changed during inspection")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise RuntimeError("runtime claim is held") from error
            raise
        renamed = os.lstat(path)
        if (
            stat.S_ISLNK(renamed.st_mode)
            or renamed.st_dev != opened.st_dev
            or renamed.st_ino != opened.st_ino
        ):
            raise ValueError("runtime claim changed during inspection")
        return "RUNTIME_CLAIM_AVAILABLE", {
            "claim_file_present": True,
            "launch_rechecks_atomically": True,
        }
    finally:
        if descriptor is not None:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _disk_status(prepared: PreparedCohort) -> tuple[str, dict[str, object]]:
    value = os.statvfs(prepared.cohort_root)
    fragment = value.f_frsize or value.f_bsize
    total = value.f_blocks * fragment
    available = value.f_bavail * fragment
    required = prepared.profile.disk_guard.required_free_bytes(total)
    if total <= 0 or available < 0 or available > total:
        raise ValueError("disk accounting returned invalid values")
    evidence = {
        "total_bytes": total,
        "available_bytes": available,
        "required_free_bytes": required,
    }
    if available < required:
        return "DISK_RESERVE_BREACHED", evidence
    return "DISK_RESERVE_AVAILABLE", evidence


def preflight_prepared_cohort(
    prepared: PreparedCohort,
    backend: RuntimeBackend,
    *,
    limits: RuntimeLimits | None = None,
    context_policy: ContextWindowPolicy | None = None,
    publisher: ContributionPublisher | None = None,
    checkers: Sequence[PreflightChecker] = (),
) -> PreflightReport:
    """Inspect launch readiness without producing any durable runtime state.

    The report is a point-in-time observation.  A PASS never replaces the
    launch's atomic claim, pin re-verification, resource guard, or publication
    identity checks.
    """

    if not isinstance(prepared, PreparedCohort):
        raise TypeError("prepared must be an authenticated PreparedCohort")
    selected_limits = RuntimeLimits() if limits is None else limits
    selected_context = (
        ContextWindowPolicy() if context_policy is None else context_policy
    )
    if not isinstance(selected_limits, RuntimeLimits):
        raise TypeError("limits must be RuntimeLimits")
    if not isinstance(selected_context, ContextWindowPolicy):
        raise TypeError("context_policy must be ContextWindowPolicy")
    if isinstance(checkers, (str, bytes)) or not isinstance(checkers, Sequence):
        raise TypeError("checkers must be a sequence")
    if len(checkers) > MAXIMUM_PREFLIGHT_CHECKS - 10:
        raise ValueError("too many preflight checkers")

    checks: list[PreflightCheck] = []
    plan_sha256 = prepared.plan.sha256
    backend_identity: BackendIdentity | None = None
    publication_identity: PublicationIdentity | None = None
    context_policy_sha256: str | None = None

    try:
        plan_sha256, evidence = _verify_plan(prepared)
        _pass(checks, "plan", "PLAN_AUTHENTICATED", evidence)
    except Exception as error:
        _fail(checks, "plan", "PLAN_AUTHENTICATION_DRIFT", _error_evidence(error))

    _pass(
        checks,
        "lifecycle_limits",
        "LIFECYCLE_LIMITS_VALID",
        selected_limits.to_value(),
    )

    try:
        backend_identity = _verify_backend_identity(backend)
        _pass(
            checks,
            "backend_identity",
            "BACKEND_IDENTITY_VALID",
            {
                "name": backend_identity.name,
                "protocol": backend_identity.protocol,
                "backend_sha256": backend_identity.sha256,
            },
        )
    except Exception as error:
        _fail(
            checks,
            "backend_identity",
            "BACKEND_IDENTITY_INVALID",
            _error_evidence(error),
        )

    try:
        _verify_backend_runtime(backend)
        _pass(
            checks,
            "backend_runtime",
            "BACKEND_RUNTIME_PINS_VERIFIED",
            {"public_verifier_invoked": True},
        )
    except Exception as error:
        _fail(
            checks,
            "backend_runtime",
            "BACKEND_RUNTIME_VERIFICATION_FAILED",
            _error_evidence(error),
        )

    session_ids = tuple(item.session_id for item in prepared.plan.sessions)
    try:
        bound_context = selected_context.bind(session_ids)
        context_policy_sha256 = hashlib.sha256(
            canonical_json(bound_context)
        ).hexdigest()
        control = getattr(
            backend,
            "context_window_control",
            ContextWindowControl.NOT_APPLICABLE,
        )
        if not isinstance(control, ContextWindowControl):
            raise TypeError("backend context-window control is invalid")
        if (
            selected_context.configured
            and control is not ContextWindowControl.NATIVE_MODEL_WINDOW
        ):
            raise ValueError("backend cannot apply the configured context window")
        validator = getattr(backend, "validate_context_window_policy", None)
        if validator is not None:
            if not callable(validator) or inspect.iscoroutinefunction(validator):
                raise TypeError("backend context policy validator is invalid")
            result = validator(selected_context, session_ids)
            if inspect.isawaitable(result) or result is not None:
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise TypeError("backend context policy validator must return None")
        configured_sessions = sum(
            selected_context.for_session(session_id) is not None
            for session_id in session_ids
        )
        _pass(
            checks,
            "context_policy",
            "CONTEXT_POLICY_SUPPORTED",
            {
                "backend_control": control.value,
                "configured": selected_context.configured,
                "configured_session_count": configured_sessions,
                "override_count": len(selected_context.session_overrides),
                "policy_sha256": context_policy_sha256,
            },
        )
    except Exception as error:
        _fail(
            checks,
            "context_policy",
            "CONTEXT_POLICY_UNSUPPORTED",
            _error_evidence(error),
        )

    try:
        publication_identity = _publication_identity(publisher)
        _pass(
            checks,
            "publication_identity",
            "PUBLICATION_IDENTITY_VALID",
            {
                "mode": publication_identity.mode,
                "protocol": publication_identity.protocol,
                "publication_sha256": publication_identity.sha256,
            },
        )
    except Exception as error:
        _fail(
            checks,
            "publication_identity",
            "PUBLICATION_IDENTITY_INVALID",
            _error_evidence(error),
        )

    runtime_root = prepared.cohort_root / "runtime"
    if runtime_root.exists() or runtime_root.is_symlink():
        _fail(
            checks,
            "runtime_absent",
            "RUNTIME_ALREADY_EXISTS",
            {"runtime_path_present": True},
        )
    else:
        _pass(
            checks,
            "runtime_absent",
            "RUNTIME_NOT_CREATED",
            {"runtime_path_present": False},
        )

    try:
        claim_code, evidence = _claim_path_status(prepared.cohort_root)
        _pass(checks, "runtime_claim", claim_code, evidence)
    except Exception as error:
        _fail(
            checks,
            "runtime_claim",
            "RUNTIME_CLAIM_UNAVAILABLE",
            _error_evidence(error),
        )

    try:
        profile = prepared.profile
        if (
            profile.name != prepared.plan.safety_profile
            or profile.sha256 != prepared.plan.safety_profile_sha256
        ):
            raise ValueError("safety profile identity drift")
        _pass(
            checks,
            "safety_profile",
            "SAFETY_PROFILE_BOUND",
            {
                "name": profile.name,
                "sha256": profile.sha256,
                "workspace_scan_mode": profile.workspace.scan_mode,
                "runtime_cache_scan_mode": profile.runtime_cache.scan_mode,
            },
        )
    except Exception as error:
        _fail(
            checks,
            "safety_profile",
            "SAFETY_PROFILE_INVALID",
            _error_evidence(error),
        )

    try:
        disk_code, evidence = _disk_status(prepared)
        if disk_code == "DISK_RESERVE_BREACHED":
            _fail(checks, "disk_reserve", disk_code, evidence)
        else:
            _pass(checks, "disk_reserve", disk_code, evidence)
    except Exception as error:
        _fail(
            checks,
            "disk_reserve",
            "DISK_ACCOUNTING_UNAVAILABLE",
            _error_evidence(error),
        )

    checker_names: set[str] = set()
    for checker in checkers:
        try:
            name = checker.name
            if type(name) is not str or _SAFE_HOOK_NAME.fullmatch(name) is None:
                raise ValueError("preflight checker name is invalid")
            if name in checker_names:
                raise ValueError("preflight checker name is duplicated")
            checker_names.add(name)
            if publication_identity is None:
                raise ValueError("publication identity is unavailable")
            result = checker.verify(
                prepared=prepared,
                backend=backend,
                limits=selected_limits,
                context_policy=selected_context,
                publication_identity=publication_identity,
            )
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise TypeError("preflight checker returned an awaitable")
            evidence = {} if result is None else result
            _pass(checks, f"hook.{name}", "EXTENSION_CHECK_PASSED", evidence)
        except Exception as error:
            try:
                fallback = getattr(checker, "name", "invalid")
            except Exception:
                fallback = "invalid"
            if type(fallback) is not str or _SAFE_HOOK_NAME.fullmatch(fallback) is None:
                fallback = f"invalid-{len(checker_names) + 1}"
            check_name = f"hook.{fallback}"
            if any(item.name == check_name for item in checks):
                check_name = f"hook.invalid-{len(checks)}"
            _fail(
                checks,
                check_name,
                "EXTENSION_CHECK_FAILED",
                _error_evidence(error),
            )

    return PreflightReport(
        cohort_id=prepared.plan.cohort_id,
        plan_sha256=plan_sha256,
        backend_sha256=(
            None if backend_identity is None else backend_identity.sha256
        ),
        publication_sha256=(
            None if publication_identity is None else publication_identity.sha256
        ),
        context_policy_sha256=context_policy_sha256,
        checks=tuple(checks),
    )


__all__ = [
    "MAXIMUM_PREFLIGHT_CHECKS",
    "MAXIMUM_PREFLIGHT_CHECK_EVIDENCE_BYTES",
    "MAXIMUM_PREFLIGHT_REPORT_BYTES",
    "PREFLIGHT_REPORT_SCHEMA",
    "PreflightCheck",
    "PreflightChecker",
    "PreflightReport",
    "preflight_prepared_cohort",
]
