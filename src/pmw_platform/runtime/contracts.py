"""Backend-neutral contracts for real research session runtimes.

The host owns launch identity and terminal classification.  A backend may
start work and report an outcome, but it never receives a PMW writer and it
cannot choose its session identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, runtime_checkable

from ..sessions import SessionSpec
from ..world import ResearchContribution
from ..world.records import canonical_json
from .context import (
    MAXIMUM_CONTEXT_WINDOW_TOKENS,
    ContextWindowControl,
)
from .usage import (
    PROVENANCE_BACKEND_DECLARED_NO_USAGE_EVIDENCE,
    UsageEvidence,
)


BACKEND_OUTCOME_SCHEMA = "PMW_RUNTIME_BACKEND_OUTCOME_1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROTOCOL = re.compile(r"^[A-Z][A-Z0-9._-]{0,127}$")
_TERMINAL_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
# Public identity may truthfully say ``auth_kind: oauth``.  Reject keys which
# conventionally carry the credential value itself, not harmless provenance.
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:access[_-]?token|refresh[_-]?token|bearer[_-]?token|"
    r"api[_-]?key|client[_-]?secret|password|credential[_-]?value|"
    r"private[_-]?key|session[_-]?secret)(?:$|[_-])",
    re.IGNORECASE,
)
MAXIMUM_PUBLIC_CONFIG_BYTES = 1_048_576
MAXIMUM_BACKEND_SUMMARY_BYTES = 65_536
MAXIMUM_BACKEND_METADATA_BYTES = 1_048_576
MAXIMUM_CONTRIBUTIONS = 64
MAXIMUM_STOP_GRACE_SECONDS = 300.0


def runtime_host_policy_value() -> dict[str, object]:
    """Return the exact backend-neutral host policy bound into a launch."""

    return {
        "platform_protocol": "PMW_RESEARCH_PLATFORM_RUNTIME_1",
        "context_window": {
            "authority": "IMMUTABLE_LAUNCH_PER_SESSION",
            "unset": "BACKEND_DECLARED_MODEL_WINDOW",
            "enforcement": "BACKEND_CAPABILITY_REQUIRED_WHEN_CONFIGURED",
            "strict_pre_http_input_gate": "NOT_CLAIMED",
        },
        "terminal_authority": "HOST",
        "unknown_retains_slot": True,
        "receipt_authority": "DURABLE_STORE",
        "resource_accounting": {
            "tree_limits": "AGGREGATE_BYTES_ENTRIES_DEPTH",
            "scan_schedule": "INITIAL_TERMINAL_AND_PROFILE_LIVE",
            "hardlinks": "BYTE_DEDUPLICATED_NOT_REJECTED",
            "single_file_limit": "NOT_ENFORCED",
        },
    }


class RuntimeContractError(ValueError):
    """A backend contract value is malformed or crosses a trust boundary."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _json_clone(value: object, *, maximum_bytes: int, label: str) -> object:
    try:
        encoded = canonical_json(value)
    except Exception as error:
        raise RuntimeContractError("MALFORMED_RUNTIME_JSON", label) from error
    if len(encoded) > maximum_bytes:
        raise RuntimeContractError("RUNTIME_VALUE_TOO_LARGE", label)
    return json.loads(encoded.decode("utf-8"))


def _reject_sensitive_keys(value: object) -> None:
    stack = [value]
    while stack:
        selected = stack.pop()
        if type(selected) is dict:
            for key, child in selected.items():
                if type(key) is not str:
                    raise RuntimeContractError(
                        "MALFORMED_BACKEND_IDENTITY", "non-text config key"
                    )
                if _SENSITIVE_KEY.search(key):
                    raise RuntimeContractError(
                        "SECRET_IN_PUBLIC_IDENTITY", key[:128]
                    )
                stack.append(child)
        elif type(selected) is list:
            stack.extend(selected)


@dataclass(frozen=True, init=False)
class BackendIdentity:
    """Stable public backend identity; it must never contain secret values."""

    name: str
    protocol: str
    _public_config_bytes: bytes = field(repr=False)

    def __init__(
        self,
        *,
        name: str,
        protocol: str,
        public_config: Mapping[str, object],
    ) -> None:
        if type(name) is not str or _IDENTIFIER.fullmatch(name) is None:
            raise RuntimeContractError("MALFORMED_BACKEND_IDENTITY", "name")
        if type(protocol) is not str or _PROTOCOL.fullmatch(protocol) is None:
            raise RuntimeContractError("MALFORMED_BACKEND_IDENTITY", "protocol")
        if type(public_config) is not dict:
            raise RuntimeContractError(
                "MALFORMED_BACKEND_IDENTITY", "public_config"
            )
        cloned = _json_clone(
            public_config,
            maximum_bytes=MAXIMUM_PUBLIC_CONFIG_BYTES,
            label="public_config",
        )
        _reject_sensitive_keys(cloned)
        encoded = canonical_json(cloned)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "_public_config_bytes", encoded)

    @property
    def public_config(self) -> dict[str, object]:
        value = json.loads(self._public_config_bytes.decode("utf-8"))
        if type(value) is not dict:
            raise AssertionError("backend public config is not an object")
        return value

    def to_value(self) -> dict[str, object]:
        return {
            "name": self.name,
            "protocol": self.protocol,
            "public_config": self.public_config,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.to_value())).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionRequest:
    """Host-authenticated request passed to exactly one backend session."""

    plan_sha256: str
    launch_sha256: str
    spec: SessionSpec
    briefing_path: Path
    invocation_path: Path
    private_root: Path
    workspace: Path
    cache: Path
    evidence: Path
    session_wall_seconds: float | None
    stop_grace_seconds: float
    context_window_tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.spec, SessionSpec):
            raise TypeError("spec must come from an authenticated CohortPlan")
        for label in ("plan_sha256", "launch_sha256"):
            value = getattr(self, label)
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise RuntimeContractError("MALFORMED_SESSION_REQUEST", label)
        for label in (
            "briefing_path",
            "invocation_path",
            "private_root",
            "workspace",
            "cache",
            "evidence",
        ):
            if not isinstance(getattr(self, label), Path):
                raise TypeError(f"{label} must be a Path")
        if self.session_wall_seconds is not None and (
            type(self.session_wall_seconds) not in {int, float}
            or not math.isfinite(float(self.session_wall_seconds))
            or self.session_wall_seconds <= 0
        ):
            raise RuntimeContractError(
                "MALFORMED_SESSION_REQUEST", "session_wall_seconds"
            )
        if (
            type(self.stop_grace_seconds) not in {int, float}
            or not math.isfinite(float(self.stop_grace_seconds))
            or self.stop_grace_seconds <= 0
            or self.stop_grace_seconds > MAXIMUM_STOP_GRACE_SECONDS
        ):
            raise RuntimeContractError(
                "MALFORMED_SESSION_REQUEST", "stop_grace_seconds"
            )
        if self.context_window_tokens is not None and (
            type(self.context_window_tokens) is not int
            or self.context_window_tokens <= 0
            or self.context_window_tokens > MAXIMUM_CONTEXT_WINDOW_TOKENS
        ):
            raise RuntimeContractError(
                "MALFORMED_SESSION_REQUEST", "context_window_tokens"
            )


@dataclass(frozen=True, slots=True)
class StopProof:
    """Backend evidence for whether side effects are proven stopped."""

    stopped: bool
    reason: str
    forced: bool = False
    process_group_id: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if type(self.stopped) is not bool or type(self.forced) is not bool:
            raise TypeError("stop proof flags must be booleans")
        if type(self.reason) is not str or _TERMINAL_REASON.fullmatch(self.reason) is None:
            raise RuntimeContractError("MALFORMED_STOP_PROOF", "reason")
        if self.process_group_id is not None and (
            type(self.process_group_id) is not int or self.process_group_id <= 0
        ):
            raise RuntimeContractError("MALFORMED_STOP_PROOF", "process_group_id")
        if type(self.detail) is not str or len(self.detail.encode("utf-8")) > 2_048:
            raise RuntimeContractError("MALFORMED_STOP_PROOF", "detail")

    def to_value(self) -> dict[str, object]:
        return {
            "stopped": self.stopped,
            "reason": self.reason,
            "forced": self.forced,
            "process_group_id": self.process_group_id,
            "detail": self.detail,
        }


@dataclass(frozen=True, init=False)
class BackendOutcome:
    """A bounded backend report; the host still decides terminal status.

    ``usage`` stays free-form because the session's own result envelope writes
    it, and nothing in that envelope is trusted as a measurement.
    ``usage_evidence`` is the opposite: only trusted adapter code sets it, it
    never travels through :meth:`from_value`, and it always states whether its
    numbers were measured, merely asserted, or absent.
    """

    success: bool
    terminal_reason: str
    summary: str
    contributions: tuple[ResearchContribution, ...]
    usage_evidence: UsageEvidence
    _usage_bytes: bytes = field(repr=False)
    _evidence_bytes: bytes = field(repr=False)

    def __init__(
        self,
        *,
        success: bool,
        terminal_reason: str,
        summary: str,
        contributions: tuple[ResearchContribution, ...] = (),
        usage: Mapping[str, object] | None = None,
        evidence: Mapping[str, object] | None = None,
        usage_evidence: UsageEvidence | None = None,
    ) -> None:
        if type(success) is not bool:
            raise RuntimeContractError("MALFORMED_BACKEND_OUTCOME", "success")
        if (
            type(terminal_reason) is not str
            or _TERMINAL_REASON.fullmatch(terminal_reason) is None
        ):
            raise RuntimeContractError(
                "MALFORMED_BACKEND_OUTCOME", "terminal_reason"
            )
        if type(summary) is not str or len(summary.encode("utf-8")) > MAXIMUM_BACKEND_SUMMARY_BYTES:
            raise RuntimeContractError("MALFORMED_BACKEND_OUTCOME", "summary")
        if not isinstance(contributions, tuple) or len(contributions) > MAXIMUM_CONTRIBUTIONS:
            raise RuntimeContractError(
                "MALFORMED_BACKEND_OUTCOME", "contributions"
            )
        if any(not isinstance(item, ResearchContribution) for item in contributions):
            raise RuntimeContractError(
                "MALFORMED_BACKEND_OUTCOME", "contribution type"
            )
        selected_usage = {} if usage is None else usage
        selected_evidence = {} if evidence is None else evidence
        if type(selected_usage) is not dict or type(selected_evidence) is not dict:
            raise RuntimeContractError("MALFORMED_BACKEND_OUTCOME", "metadata")
        # An outcome that says nothing about tokens has measured nothing.  The
        # default is the honest marker, never an implied zero.
        selected_usage_evidence = (
            UsageEvidence.unmeasured(
                provenance=PROVENANCE_BACKEND_DECLARED_NO_USAGE_EVIDENCE,
                detail=(
                    "the backend reported no typed usage evidence for this "
                    "outcome"
                ),
            )
            if usage_evidence is None
            else usage_evidence
        )
        if not isinstance(selected_usage_evidence, UsageEvidence):
            raise RuntimeContractError(
                "MALFORMED_BACKEND_OUTCOME", "usage_evidence"
            )
        usage_clone = _json_clone(
            selected_usage,
            maximum_bytes=MAXIMUM_BACKEND_METADATA_BYTES,
            label="usage",
        )
        evidence_clone = _json_clone(
            selected_evidence,
            maximum_bytes=MAXIMUM_BACKEND_METADATA_BYTES,
            label="evidence",
        )
        object.__setattr__(self, "success", success)
        object.__setattr__(self, "terminal_reason", terminal_reason)
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "contributions", contributions)
        object.__setattr__(self, "usage_evidence", selected_usage_evidence)
        object.__setattr__(self, "_usage_bytes", canonical_json(usage_clone))
        object.__setattr__(self, "_evidence_bytes", canonical_json(evidence_clone))

    @property
    def usage(self) -> dict[str, object]:
        value = json.loads(self._usage_bytes.decode("utf-8"))
        if type(value) is not dict:
            raise AssertionError("usage is not an object")
        return value

    @property
    def evidence(self) -> dict[str, object]:
        value = json.loads(self._evidence_bytes.decode("utf-8"))
        if type(value) is not dict:
            raise AssertionError("evidence is not an object")
        return value

    def to_value(self) -> dict[str, object]:
        return {
            "schema": BACKEND_OUTCOME_SCHEMA,
            "success": self.success,
            "terminal_reason": self.terminal_reason,
            "summary": self.summary,
            "usage": self.usage,
            "evidence": self.evidence,
            "contributions": [item.to_value() for item in self.contributions],
        }

    @classmethod
    def from_value(cls, value: object) -> "BackendOutcome":
        """Validate the bounded identity-free result written by a backend.

        The envelope schema deliberately has no ``usage_evidence`` field: a
        session must not be able to write its own token measurement into a
        receipt.  Every outcome parsed here starts ``UNMEASURED``, and only the
        trusted adapter that watched the transport may attach a measured block.
        """

        expected = {
            "schema",
            "success",
            "terminal_reason",
            "summary",
            "usage",
            "evidence",
            "contributions",
        }
        if type(value) is not dict or set(value) != expected:
            raise RuntimeContractError("MALFORMED_BACKEND_OUTCOME", "fields")
        if value.get("schema") != BACKEND_OUTCOME_SCHEMA:
            raise RuntimeContractError("MALFORMED_BACKEND_OUTCOME", "schema")
        raw_contributions = value.get("contributions")
        if type(raw_contributions) is not list:
            raise RuntimeContractError(
                "MALFORMED_BACKEND_OUTCOME", "contributions"
            )
        try:
            contributions = tuple(
                ResearchContribution.from_value(item)
                for item in raw_contributions
            )
        except Exception as error:
            raise RuntimeContractError(
                "MALFORMED_BACKEND_OUTCOME", "contribution"
            ) from error
        return cls(
            success=value.get("success"),  # type: ignore[arg-type]
            terminal_reason=value.get("terminal_reason"),  # type: ignore[arg-type]
            summary=value.get("summary"),  # type: ignore[arg-type]
            contributions=contributions,
            usage=value.get("usage"),  # type: ignore[arg-type]
            evidence=value.get("evidence"),  # type: ignore[arg-type]
        )


class BackendStartError(RuntimeError):
    """A failed start together with any available no-process proof."""

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        stop_proof: StopProof | None = None,
    ) -> None:
        self.code = code
        self.detail = detail[:2_000]
        self.stop_proof = stop_proof
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


@runtime_checkable
class RunningSession(Protocol):
    """One started backend handle with bounded, idempotent stop semantics.

    A runtime backend is trusted host code, not an agent plugin. ``stop`` must
    honor the supplied grace period, must not perform blocking synchronous
    work on the event loop, and must settle all adapter-owned cleanup before
    returning or raising.  It may not leave a hidden process-cleanup task for
    the host to abandon.  Untrusted research work belongs in the backend's
    managed process/VM.
    """

    async def wait(self) -> BackendOutcome: ...

    async def stop(self, reason: str, grace_seconds: float) -> StopProof: ...


@runtime_checkable
class RuntimeBackend(Protocol):
    """Pluggable trusted transport boundary used by the host orchestrator.

    ``start`` must close its process-creation cancellation gap: cancellation
    before a handle is returned must either have no external side effect or
    raise :class:`BackendStartError` with a positive/negative ``StopProof``.
    It must remain cancellable and may not hide blocking side-effecting work
    in a thread.  Bounded, read-only pin verification may use a worker thread
    when cancellation joins that worker before the handoff is classified.
    """

    @property
    def identity(self) -> BackendIdentity: ...

    @property
    def context_window_control(self) -> ContextWindowControl: ...

    def verify_runtime(self) -> None:
        """Synchronously recheck local runtime pins without starting work."""

        ...

    async def start(self, request: SessionRequest) -> RunningSession: ...
