"""Backend-neutral orchestration for one authenticated research cohort.

The mathematical plan says *which* sessions exist.  This module creates a
separate launch identity saying *how* those sessions are run, then owns every
terminal classification and durable receipt.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
from pathlib import Path
from typing import Awaitable, Mapping, Protocol, Sequence

from ..sessions import SessionSpec, SessionStatus
from ..verifier_kit import (
    VerifierKit,
    absent_verifier_kit_announcement,
    disabled_verifier_kit_launch_value,
    disabled_verifier_kit_session_evidence,
    read_session_verifier_kit_evidence,
    unreadable_verifier_kit_session_evidence,
)
from ..world import ResearchContribution
from ..world.records import canonical_json
from .auth import PreparedCohort, authenticate_plan_bundle
from .contracts import (
    BackendIdentity,
    BackendOutcome,
    BackendStartError,
    MAXIMUM_STOP_GRACE_SECONDS,
    RunningSession,
    RuntimeBackend,
    SessionRequest,
    StopProof,
    runtime_host_policy_value,
)
from .context import (
    CONTEXT_WINDOW_SEMANTICS,
    ContextWindowControl,
    ContextWindowPolicy,
)
from .publish import PublicationIdentity
from .readiness import (
    RequiredReadinessChecker,
    RequiredReadinessIdentity,
    verify_required_readiness,
)
from .resource_guard import ResourceEvent, ResourceGuard
from .store import RuntimeClaim, RuntimeStore, RuntimeStoreError
from .usage import PROVENANCE_NO_BACKEND_OUTCOME, UsageEvidence


RUNTIME_LAUNCH_SCHEMA = "PMW_RUNTIME_LAUNCH_1"
RUNTIME_INVOCATION_SCHEMA = "PMW_RUNTIME_SESSION_INVOCATION_1"
RUNTIME_STATE_SCHEMA = "PMW_RUNTIME_SESSION_STATE_1"
RUNTIME_RECEIPT_SCHEMA = "PMW_RUNTIME_SESSION_RECEIPT_1"
RUNTIME_SETTLEMENT_SCHEMA = "PMW_RUNTIME_SETTLEMENT_1"

# Agenda-arm vocabulary held as literals for the same reason the durable store
# holds the verifier-kit vocabulary: an arm is an experiment, and the session
# runtime must not import one to describe its own absence.  A test pins these
# against the producing module's constants.
AGENDA_ARM_LAUNCH_SCHEMA = "PMW_AGENDA_ARM_LAUNCH_1"
AGENDA_ARM_EVIDENCE_SCHEMA = "PMW_AGENDA_ARM_SESSION_EVIDENCE_1"
AGENDA_ARM_ENFORCEMENT_SCOPE = (
    "PUBLICATION_TIME_RECORD_VALIDATION_ONLY_NO_RESEARCH_BEHAVIOUR_POLICING"
)


class RuntimeOrchestrationError(RuntimeError):
    """The host could not produce a complete trustworthy settlement."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


class RuntimePublicationError(RuntimeOrchestrationError):
    """A publish sequence failed after zero or more durable admissions."""

    def __init__(
        self,
        code: str,
        publications: tuple[object, ...],
    ) -> None:
        self.publications = publications
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    """Launch-time lifecycle limits, independent of model context policy."""

    startup_seconds: float = 60.0
    session_wall_seconds: float | None = 86_400.0
    stop_grace_seconds: float = 10.0

    def __post_init__(self) -> None:
        for label in ("startup_seconds", "stop_grace_seconds"):
            value = getattr(self, label)
            if (
                type(value) not in {int, float}
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"{label} must be positive and finite")
        if self.stop_grace_seconds > MAXIMUM_STOP_GRACE_SECONDS:
            raise ValueError(
                f"stop_grace_seconds must be <= {MAXIMUM_STOP_GRACE_SECONDS:g}"
            )
        if self.session_wall_seconds is not None and (
            type(self.session_wall_seconds) not in {int, float}
            or not math.isfinite(float(self.session_wall_seconds))
            or self.session_wall_seconds <= 0
        ):
            raise ValueError("session_wall_seconds must be positive and finite")

    def to_value(self) -> dict[str, object]:
        return {
            "startup_seconds": float(self.startup_seconds),
            "session_wall_seconds": (
                None
                if self.session_wall_seconds is None
                else float(self.session_wall_seconds)
            ),
            "stop_grace_seconds": float(self.stop_grace_seconds),
        }


@dataclass(frozen=True, slots=True)
class RuntimeRunResult:
    launch_sha256: str
    settlement_sha256: str
    outcome: str
    receipts: tuple[dict[str, object], ...]
    settlement: dict[str, object]


class ContributionPublisher(Protocol):
    """Trusted, bounded publisher whose public identity is frozen in launch.

    A custom asynchronous implementation must remain cancellable and bounded;
    the built-in PMW publisher is a synchronous local admission operation.
    """

    @property
    def identity(self) -> PublicationIdentity: ...

    def __call__(
        self,
        spec: SessionSpec,
        contribution: ResearchContribution,
    ) -> object | Awaitable[object]: ...


class AgendaArm(Protocol):
    """An instrument exposure the host validates against at publication time.

    The runtime deliberately holds only this protocol.  An arm is an
    *experiment* -- it decides which record shapes a launch admits -- and the
    session runtime must not depend on one, exactly as it does not depend on a
    particular publisher.  Every method must be synchronous, bounded and free of
    side effects outside the arm's own ledger.

    A review that does not admit a contribution is a recorded research event.
    The host skips that one publication, stamps the verdict into the session's
    settlement evidence, and settles the session normally.
    """

    def launch_value(self) -> Mapping[str, object]: ...

    def briefing_announcement(self) -> Mapping[str, object]: ...

    def review(self, spec: SessionSpec, contribution: object) -> object: ...

    def observe(
        self,
        spec: SessionSpec,
        contribution: object,
        result: object,
    ) -> None: ...

    def settle(self, spec: SessionSpec) -> object: ...

    def session_evidence(self, spec: SessionSpec) -> Mapping[str, object]: ...


def _arm_admits(decision: object) -> bool:
    """Read one arm decision, refusing anything that is not an explicit bool."""

    admitted = getattr(decision, "admitted", None)
    if type(admitted) is not bool:
        raise RuntimeOrchestrationError("AGENDA_ARM_DECISION_INVALID")
    return admitted


def not_configured_agenda_arm_launch_value() -> dict[str, object]:
    """Return the exact launch block used when a launch configures no arm."""

    return {
        "schema": AGENDA_ARM_LAUNCH_SCHEMA,
        "mode": "NOT_CONFIGURED",
        "reason": "NO_AGENDA_ARM_CONFIGURED",
        "enforcement": AGENDA_ARM_ENFORCEMENT_SCOPE,
    }


def not_configured_agenda_arm_session_evidence() -> dict[str, object]:
    """Return the receipt block used when a launch configured no arm."""

    return {
        "schema": AGENDA_ARM_EVIDENCE_SCHEMA,
        "mode": "NOT_CONFIGURED",
        "arm": None,
        "arm_sha256": None,
        "reviewed": 0,
        "admitted": 0,
        "rejected": 0,
        "reason": "NO_AGENDA_ARM_CONFIGURED",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pretty_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8", errors="strict")


def _safe_type(value: object) -> str:
    return f"{type(value).__module__}.{type(value).__qualname__}"[:512]


def _error_code(error: BaseException, default: str) -> str:
    value = getattr(error, "code", None)
    if type(value) is str and value and len(value) <= 128:
        return value
    return default


def _public_result(value: object) -> object:
    if hasattr(value, "to_value") and callable(value.to_value):
        value = value.to_value()
    encoded = canonical_json(value)
    if len(encoded) > 65_536:
        return {
            "projected": True,
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    return json.loads(encoded.decode("utf-8"))


def _outcome_receipt_value(outcome: BackendOutcome) -> dict[str, object]:
    return {
        "success": outcome.success,
        "terminal_reason": outcome.terminal_reason,
        "summary": outcome.summary,
        "usage": outcome.usage,
        "evidence": outcome.evidence,
        "contribution_count": len(outcome.contributions),
        "contribution_sha256": [
            hashlib.sha256(canonical_json(item.to_value())).hexdigest()
            for item in outcome.contributions
        ],
    }


def build_launch_manifest(
    prepared: PreparedCohort,
    identity: BackendIdentity,
    limits: RuntimeLimits,
    publication_identity: PublicationIdentity | None = None,
    context_policy: ContextWindowPolicy | None = None,
    context_window_control: ContextWindowControl = (
        ContextWindowControl.NOT_APPLICABLE
    ),
    required_readiness: RequiredReadinessIdentity | None = None,
    verifier_kit: VerifierKit | None = None,
    agenda_arm: AgendaArm | None = None,
) -> dict[str, object]:
    """Build the immutable execution identity, separate from the math plan."""

    if not isinstance(prepared, PreparedCohort):
        raise TypeError("prepared must be PreparedCohort")
    if not isinstance(identity, BackendIdentity):
        raise TypeError("backend identity must be BackendIdentity")
    selected_publication = (
        PublicationIdentity.disabled()
        if publication_identity is None
        else publication_identity
    )
    if not isinstance(selected_publication, PublicationIdentity):
        raise TypeError("publication identity must be PublicationIdentity")
    selected_context = (
        ContextWindowPolicy() if context_policy is None else context_policy
    )
    if not isinstance(selected_context, ContextWindowPolicy):
        raise TypeError("context_policy must be ContextWindowPolicy")
    if not isinstance(context_window_control, ContextWindowControl):
        raise TypeError("context_window_control must be ContextWindowControl")
    selected_readiness = (
        RequiredReadinessIdentity(())
        if required_readiness is None
        else required_readiness
    )
    if not isinstance(selected_readiness, RequiredReadinessIdentity):
        raise TypeError("required_readiness must be RequiredReadinessIdentity")
    if verifier_kit is not None and not isinstance(verifier_kit, VerifierKit):
        raise TypeError("verifier_kit must be VerifierKit")
    kit_value = (
        disabled_verifier_kit_launch_value()
        if verifier_kit is None
        else verifier_kit.launch_value()
    )
    arm_value = _agenda_arm_launch_value(agenda_arm)
    session_ids = [item.session_id for item in prepared.plan.sessions]
    return {
        "schema": RUNTIME_LAUNCH_SCHEMA,
        "created_at": _now(),
        "cohort_id": prepared.plan.cohort_id,
        "world_id": prepared.plan.world_id,
        "world_ref": prepared.plan.world_ref,
        "base_snapshot_ref": prepared.plan.base_snapshot_ref,
        "plan_sha256": prepared.plan.sha256,
        "briefing_sha256": prepared.plan.briefing_sha256,
        "safety_profile": prepared.profile.name,
        "safety_profile_sha256": prepared.profile.sha256,
        "core_lock_sha256": prepared.core_lock.sha256,
        "backend": identity.to_value(),
        "backend_sha256": identity.sha256,
        "publication": selected_publication.to_value(),
        "publication_sha256": selected_publication.sha256,
        "concurrency": prepared.plan.concurrency,
        "session_ids": session_ids,
        "limits": limits.to_value(),
        "context_window_policy": selected_context.bind(session_ids),
        "backend_context_window_control": context_window_control.value,
        "required_readiness": selected_readiness.to_value(),
        "required_readiness_sha256": selected_readiness.sha256,
        "verifier_kit": kit_value,
        "verifier_kit_sha256": hashlib.sha256(
            canonical_json(kit_value)
        ).hexdigest(),
        # The arm is execution identity, not mathematical identity: it says
        # which instruments this launch exposes, never what the world means.
        "agenda_arm": arm_value,
        "agenda_arm_sha256": hashlib.sha256(
            canonical_json(arm_value)
        ).hexdigest(),
        "host_policy": runtime_host_policy_value(),
    }


def _agenda_arm_launch_value(agenda_arm: AgendaArm | None) -> dict[str, object]:
    if agenda_arm is None:
        return not_configured_agenda_arm_launch_value()
    value = agenda_arm.launch_value()
    if type(value) is not dict:
        raise RuntimeOrchestrationError("AGENDA_ARM_LAUNCH_INVALID")
    return dict(value)


class _Controller:
    def __init__(
        self,
        *,
        prepared: PreparedCohort,
        backend: RuntimeBackend,
        identity: BackendIdentity,
        store: RuntimeStore,
        launch_sha256: str,
        limits: RuntimeLimits,
        context_policy: ContextWindowPolicy,
        context_window_control: ContextWindowControl,
        publisher: ContributionPublisher | None,
        publication_identity: PublicationIdentity,
        required_readiness: RequiredReadinessIdentity,
        resource_guard: ResourceGuard,
        verifier_kit: VerifierKit | None = None,
        agenda_arm: AgendaArm | None = None,
    ) -> None:
        self.prepared = prepared
        self.backend = backend
        self.identity = identity
        self.store = store
        self.launch_sha256 = launch_sha256
        self.limits = limits
        self.context_policy = context_policy
        self.context_window_control = context_window_control
        self.publisher = publisher
        self.publication_identity = publication_identity
        self.required_readiness = required_readiness
        self.resource_guard = resource_guard
        self.verifier_kit = verifier_kit
        self.agenda_arm = agenda_arm
        # One arm ledger serves the whole cohort, and a publish may await, so
        # each review/publish/observe triple is atomic: the world admits one
        # record at a time and its tick counter says so.  The lock is *per
        # contribution*, not per session: with concurrency above one, a peer's
        # records can legitimately interleave with this session's, and whether
        # a lease is still held then depends on which session settled first.
        self.agenda_lock = asyncio.Lock()
        self.stop_new = asyncio.Event()
        self.external_cancel = False
        self.unsafe = False
        self.store_failed = False
        self.receipts: dict[str, dict[str, object]] = {}
        self.receipt_sha256: dict[str, str] = {}
        self._cursor = 0
        self._cursor_lock = asyncio.Lock()
        self._revisions: dict[str, int] = {}

    def _observe_resource_event(self, event: ResourceEvent | None) -> None:
        if event is None:
            return
        if event.uncertain:
            self.unsafe = True
            self.stop_new.set()
        elif event.scope == "COHORT":
            self.stop_new.set()

    @staticmethod
    def _resource_status(
        event: ResourceEvent,
        proof: StopProof | None,
    ) -> tuple[SessionStatus, str]:
        if event.uncertain:
            return SessionStatus.UNKNOWN, "RESOURCE_ACCOUNTING_UNCERTAIN"
        if proof is not None and not proof.stopped:
            return SessionStatus.UNKNOWN, "STOP_UNPROVEN"
        return SessionStatus.FAILED, event.code

    async def _next(self) -> SessionSpec | None:
        async with self._cursor_lock:
            if self.stop_new.is_set():
                return None
            if self._cursor >= len(self.prepared.plan.sessions):
                return None
            spec = self.prepared.plan.sessions[self._cursor]
            self._cursor += 1
            return spec

    def _transition(
        self,
        spec: SessionSpec,
        state: str,
        reason: str,
    ) -> None:
        revision = self._revisions.get(spec.session_id, 0) + 1
        self._revisions[spec.session_id] = revision
        self.store.write_state(
            spec.session_id,
            {
                "schema": RUNTIME_STATE_SCHEMA,
                "launch_sha256": self.launch_sha256,
                "plan_sha256": self.prepared.plan.sha256,
                "session_id": spec.session_id,
                "state": state,
                "reason": reason,
                "revision": revision,
                "updated_at": _now(),
            },
        )

    def _invocation(self, spec: SessionSpec) -> dict[str, object]:
        invocation: dict[str, object] = {
            "schema": RUNTIME_INVOCATION_SCHEMA,
            "launch_sha256": self.launch_sha256,
            "plan_sha256": self.prepared.plan.sha256,
            "session": {
                "session_id": spec.session_id,
                "cohort_id": spec.cohort_id,
                "world_id": spec.world_id,
                "world_ref": spec.world_ref,
                "base_snapshot_ref": spec.base_snapshot_ref,
                "safety_profile": spec.safety_profile,
                "safety_profile_sha256": spec.safety_profile_sha256,
                "core_lock_sha256": spec.core_lock_sha256,
                "briefing_sha256": spec.briefing_sha256,
            },
            "lifecycle": {
                "session_wall_seconds": (
                    None
                    if self.limits.session_wall_seconds is None
                    else float(self.limits.session_wall_seconds)
                ),
                "stop_grace_seconds": float(self.limits.stop_grace_seconds),
            },
            "context_window": {
                "semantics": CONTEXT_WINDOW_SEMANTICS,
                "context_window_tokens": self.context_policy.for_session(
                    spec.session_id
                ),
                "backend_control": self.context_window_control.value,
                "strict_pre_http_input_gate": False,
            },
            "required_readiness": self.required_readiness.to_value(),
            # Announce the in-session capability on the same authenticated
            # surface every backend already reads.  It states what exists and
            # how to call it; it recommends no research route.
            "verifier_kit": (
                absent_verifier_kit_announcement()
                if self.verifier_kit is None
                else self.verifier_kit.briefing_announcement()
            ),
        }
        if self.agenda_arm is not None:
            # A configured arm's instrument set is announced on the same
            # authenticated surface, in the same non-prescriptive voice: what
            # record shapes are legal and how claiming behaves, with no route
            # and no ordering.  Absence is deliberately silent on the Agent
            # surface; launch and receipt retain the Host control evidence.
            invocation["agenda_arm"] = self._agenda_arm_announcement()
        return invocation

    def _agenda_arm_announcement(self) -> dict[str, object]:
        if self.agenda_arm is None:
            raise RuntimeOrchestrationError("AGENDA_ARM_NOT_CONFIGURED")
        value = self.agenda_arm.briefing_announcement()
        if type(value) is not dict:
            raise RuntimeOrchestrationError("AGENDA_ARM_ANNOUNCEMENT_INVALID")
        return dict(value)

    def _agenda_arm_evidence(self, spec: SessionSpec) -> dict[str, object]:
        """Settle this session's leases, then read its arm evidence.

        Releasing before reading is deliberate: the release is part of what the
        receipt reports, and a settled session must not appear to still hold a
        lease in the same document that records its settlement.
        """

        if self.agenda_arm is None:
            return not_configured_agenda_arm_session_evidence()
        self.agenda_arm.settle(spec)
        value = self.agenda_arm.session_evidence(spec)
        if type(value) is not dict:
            raise RuntimeOrchestrationError("AGENDA_ARM_EVIDENCE_INVALID")
        return dict(value)

    def _verifier_kit_evidence(self, spec: SessionSpec) -> dict[str, object]:
        if self.verifier_kit is None:
            return disabled_verifier_kit_session_evidence()
        try:
            workspace = self.store.session_paths(spec.session_id).workspace
            return read_session_verifier_kit_evidence(self.verifier_kit, workspace)
        except BaseException:
            # An unreadable advisory ledger must never break settlement, and
            # must never be reported as a measured zero.
            return unreadable_verifier_kit_session_evidence(self.verifier_kit)

    def _request(self, spec: SessionSpec) -> SessionRequest:
        paths = self.store.session_paths(spec.session_id)
        if self.verifier_kit is not None:
            self.verifier_kit.materialize(paths.workspace)
        briefing_path = self.store.write_input_file(
            spec.session_id,
            "briefing.json",
            self.prepared.briefing_bytes,
        )
        invocation_path = self.store.write_input_file(
            spec.session_id,
            "invocation.json",
            _pretty_bytes(self._invocation(spec)),
        )
        return SessionRequest(
            plan_sha256=self.prepared.plan.sha256,
            launch_sha256=self.launch_sha256,
            spec=spec,
            briefing_path=briefing_path,
            invocation_path=invocation_path,
            private_root=paths.private,
            workspace=paths.workspace,
            cache=paths.cache,
            evidence=paths.evidence,
            session_wall_seconds=(
                None
                if self.limits.session_wall_seconds is None
                else float(self.limits.session_wall_seconds)
            ),
            stop_grace_seconds=float(self.limits.stop_grace_seconds),
            context_window_tokens=self.context_policy.for_session(
                spec.session_id
            ),
        )

    async def _persist(
        self,
        spec: SessionSpec,
        *,
        status: SessionStatus,
        terminal_reason: str,
        started_at: str | None,
        outcome: BackendOutcome | None = None,
        stop_proof: StopProof | None = None,
        publications: tuple[object, ...] = (),
        error: BaseException | None = None,
        error_code: str | None = None,
    ) -> None:
        resource_event = await self.resource_guard.finish(spec.session_id)
        self._observe_resource_event(resource_event)
        if resource_event is not None:
            if resource_event.uncertain:
                status = SessionStatus.UNKNOWN
                terminal_reason = "RESOURCE_ACCOUNTING_UNCERTAIN"
                error_code = "RESOURCE_ACCOUNTING_UNCERTAIN"
            elif started_at is not None and status is not SessionStatus.UNKNOWN:
                status = SessionStatus.FAILED
                terminal_reason = resource_event.code
                error_code = resource_event.code
            elif terminal_reason == "NOT_STARTED":
                # A cohort-wide preflight event explains why this queued
                # session never started; do not bury that cause in evidence.
                terminal_reason = resource_event.code
                error_code = resource_event.code
        receipt: dict[str, object] = {
            "schema": RUNTIME_RECEIPT_SCHEMA,
            "launch_sha256": self.launch_sha256,
            "plan_sha256": self.prepared.plan.sha256,
            "cohort_id": spec.cohort_id,
            "session_id": spec.session_id,
            "world_id": spec.world_id,
            "world_ref": spec.world_ref,
            "base_snapshot_ref": spec.base_snapshot_ref,
            "backend_sha256": self.identity.sha256,
            "status": status.value,
            "terminal_reason": terminal_reason,
            "started_at": started_at,
            "finished_at": _now(),
            "stop_proof": None if stop_proof is None else stop_proof.to_value(),
            "outcome": None if outcome is None else _outcome_receipt_value(outcome),
            "publications": list(publications),
            "error": (
                None
                if error is None and error_code is None
                else {
                    "code": error_code
                    or _error_code(error, "RUNTIME_SESSION_FAILED"),  # type: ignore[arg-type]
                    "type": None if error is None else _safe_type(error),
                }
            ),
            "resource_guard": self.resource_guard.evidence(spec.session_id),
            # A receipt always states its usage epistemics: measured with a
            # named provenance, asserted by a profile, or explicitly
            # unmeasured.  A session that never produced an outcome measured
            # nothing, and says exactly that.
            "usage": (
                UsageEvidence.unmeasured(
                    provenance=PROVENANCE_NO_BACKEND_OUTCOME,
                    detail="no backend outcome was produced for this session",
                ).to_value()
                if outcome is None
                else outcome.usage_evidence.to_value()
            ),
            "verifier_kit": self._verifier_kit_evidence(spec),
            # Settlement evidence for the arm: which instruments this session
            # used, every verdict it received, its route declarations and the
            # leases its settlement released.
            "agenda_arm": self._agenda_arm_evidence(spec),
            "context_window": {
                "semantics": CONTEXT_WINDOW_SEMANTICS,
                "configured_tokens": self.context_policy.for_session(
                    spec.session_id
                ),
                "backend_control": self.context_window_control.value,
                "strict_pre_http_input_gate": False,
            },
        }
        try:
            digest = self.store.write_receipt(spec.session_id, receipt)
        except BaseException:
            self.store_failed = True
            self.stop_new.set()
            return
        self.receipts[spec.session_id] = receipt
        self.receipt_sha256[spec.session_id] = digest
        try:
            self._transition(spec, status.value, terminal_reason)
        except BaseException:
            # receipt.json is authoritative; state.json is only a live view.
            pass

    async def _stop(
        self,
        handle: RunningSession,
        reason: str,
    ) -> StopProof:
        try:
            # ``stop`` is a trusted, contractually bounded adapter operation.
            # Never time it out and abandon adapter-owned cleanup: a durable
            # settlement is written only after the adapter has returned a
            # proof or raised while no hidden cleanup task remains.
            proof = await handle.stop(
                reason, float(self.limits.stop_grace_seconds)
            )
            if not isinstance(proof, StopProof):
                return StopProof(
                    stopped=False,
                    reason="STOP_UNPROVEN",
                    detail=f"invalid stop proof type: {_safe_type(proof)}",
                )
            return proof
        except BaseException as error:
            return StopProof(
                stopped=False,
                reason="STOP_UNPROVEN",
                detail=_safe_type(error),
            )

    async def _publish(
        self,
        spec: SessionSpec,
        outcome: BackendOutcome,
    ) -> tuple[object, ...]:
        if not outcome.contributions:
            return ()
        if self.publisher is None:
            raise RuntimeOrchestrationError("PMW_PUBLISH_UNAVAILABLE")
        published: list[object] = []
        # Without an arm there is no shared ledger to protect, and the
        # publication path stays exactly as it was.
        serialize = (
            self.agenda_lock if self.agenda_arm is not None else nullcontext()
        )
        for contribution in outcome.contributions:
            try:
                async with serialize:
                    # Check drift before consulting the arm: a decision spent
                    # on a record this host is about to refuse to publish would
                    # sit in the evidence describing an admission that never
                    # happened.
                    current_identity = getattr(self.publisher, "identity", None)
                    if (
                        not isinstance(current_identity, PublicationIdentity)
                        or current_identity.sha256
                        != self.publication_identity.sha256
                    ):
                        raise RuntimeOrchestrationError(
                            "PUBLICATION_IDENTITY_DRIFT"
                        )
                    if not self._admitted_by_arm(spec, contribution):
                        # A rejected instrument is a research event, not an
                        # apparatus failure: skip this one publication, keep the
                        # verdict, and let the session settle normally.
                        continue
                    result = self.publisher(spec, contribution)
                    if inspect.isawaitable(result):
                        result = await result
                    published.append(_public_result(result))
                    if self.agenda_arm is not None:
                        self.agenda_arm.observe(spec, contribution, result)
                    current_identity = getattr(self.publisher, "identity", None)
                    if (
                        not isinstance(current_identity, PublicationIdentity)
                        or current_identity.sha256
                        != self.publication_identity.sha256
                    ):
                        raise RuntimeOrchestrationError(
                            "PUBLICATION_IDENTITY_DRIFT"
                        )
            except BaseException as error:
                raise RuntimePublicationError(
                    _error_code(error, "PMW_PUBLISH_FAILED"),
                    tuple(published),
                ) from error
        return tuple(published)

    def _admitted_by_arm(
        self,
        spec: SessionSpec,
        contribution: ResearchContribution,
    ) -> bool:
        """Review one candidate against the arm and the then-current snapshot."""

        if self.agenda_arm is None:
            return True
        return _arm_admits(self.agenda_arm.review(spec, contribution))

    async def _start(
        self,
        request: SessionRequest,
    ) -> tuple[RunningSession | None, BaseException | None, StopProof | None]:
        def identity_matches() -> bool:
            try:
                current = self.backend.identity
            except BaseException:
                return False
            return (
                isinstance(current, BackendIdentity)
                and current.sha256 == self.identity.sha256
            )

        async def accept_result(
            selected: object,
        ) -> tuple[RunningSession | None, BaseException | None, StopProof | None]:
            if isinstance(selected, BaseException):
                return None, selected, getattr(selected, "stop_proof", None)
            if not isinstance(selected, RunningSession):
                return (
                    None,
                    RuntimeOrchestrationError("BACKEND_HANDLE_INVALID"),
                    None,
                )
            if not identity_matches():
                proof = await self._stop(selected, "BACKEND_IDENTITY_DRIFT")
                return (
                    None,
                    RuntimeOrchestrationError("BACKEND_IDENTITY_DRIFT"),
                    proof,
                )
            return selected, None, None

        async def cancel_and_join(
            task: asyncio.Task[RunningSession],
            error: BaseException,
            reason: str,
        ) -> tuple[RunningSession | None, BaseException | None, StopProof | None]:
            task.cancel()
            selected = (await asyncio.gather(task, return_exceptions=True))[0]
            accepted, returned_error, proof = await accept_result(selected)
            if accepted is not None:
                # A contract-violating backend may swallow cancellation and
                # publish a late handle.  It is still recoverable: stop and
                # join it instead of discarding an external side effect.
                proof = await self._stop(accepted, reason)
            return None, error if returned_error is None else returned_error, proof

        if not identity_matches():
            return (
                None,
                RuntimeOrchestrationError("BACKEND_IDENTITY_DRIFT"),
                StopProof(stopped=True, reason="NOT_STARTED"),
            )
        task = asyncio.create_task(self.backend.start(request))
        signal = asyncio.create_task(self.stop_new.wait())
        resource_signal = asyncio.create_task(
            self.resource_guard.wait(request.spec.session_id)
        )
        try:
            done, _pending = await asyncio.wait(
                {task, signal, resource_signal},
                timeout=float(self.limits.startup_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if resource_signal in done:
                self._observe_resource_event(resource_signal.result())
            if task in done:
                try:
                    selected_result: object = task.result()
                except BaseException as error:
                    selected_result = error
                return await accept_result(selected_result)

            if not signal.done() and not resource_signal.done():
                return await cancel_and_join(
                    task, asyncio.TimeoutError(), "START_TIMEOUT"
                )
            resource_event = self.resource_guard.event_for(
                request.spec.session_id
            )
            reason = (
                resource_event.code
                if resource_event is not None
                else "COHORT_STOP"
            )
            return await cancel_and_join(
                task,
                RuntimeOrchestrationError(reason),
                reason,
            )
        finally:
            for selected_signal in (signal, resource_signal):
                if not selected_signal.done():
                    selected_signal.cancel()
            await asyncio.gather(
                signal, resource_signal, return_exceptions=True
            )

    async def run_one(self, spec: SessionSpec) -> None:
        started_at: str | None = None
        handle: RunningSession | None = None
        wait_task: asyncio.Task[BackendOutcome] | None = None
        stop_task: asyncio.Task[bool] | None = None
        wall_task: asyncio.Task[object] | None = None
        resource_task: asyncio.Task[ResourceEvent] | None = None
        try:
            self._transition(spec, "PREPARING", "INPUT_MATERIALIZATION")
            request = self._request(spec)
            resource_event = await self.resource_guard.activate(spec.session_id)
            self._observe_resource_event(resource_event)
            if resource_event is not None:
                proof = StopProof(stopped=True, reason="NOT_STARTED")
                status, terminal_reason = self._resource_status(
                    resource_event, proof
                )
                await self._persist(
                    spec,
                    status=status,
                    terminal_reason=terminal_reason,
                    started_at=None,
                    stop_proof=proof,
                    error_code=resource_event.code,
                )
                return
            if self.stop_new.is_set():
                await self._persist_not_started(spec)
                return
            self._transition(spec, "STARTING", "BACKEND_START")
            handle, start_error, start_proof = await self._start(request)
            if handle is None:
                proven = start_proof is not None and start_proof.stopped
                resource_event = self.resource_guard.event_for(spec.session_id)
                self._observe_resource_event(resource_event)
                if not proven:
                    self.unsafe = True
                    self.stop_new.set()
                if resource_event is not None:
                    if resource_event.uncertain or not proven:
                        status = SessionStatus.UNKNOWN
                        terminal_reason = (
                            "RESOURCE_ACCOUNTING_UNCERTAIN"
                            if resource_event.uncertain and proven
                            else "STOP_UNPROVEN"
                        )
                    else:
                        status = (
                            SessionStatus.CANCELLED
                            if resource_event.scope == "COHORT"
                            else SessionStatus.FAILED
                        )
                        terminal_reason = resource_event.code
                    selected_error_code = resource_event.code
                else:
                    selected_error_code = _error_code(
                        start_error, "BACKEND_START_FAILED"  # type: ignore[arg-type]
                    )
                    status = (
                        SessionStatus.CANCELLED
                        if self.stop_new.is_set() and proven
                        else SessionStatus.FAILED
                        if proven
                        else SessionStatus.UNKNOWN
                    )
                    terminal_reason = (
                        "NOT_STARTED"
                        if self.stop_new.is_set() and proven
                        else "BACKEND_IDENTITY_DRIFT"
                        if proven
                        and selected_error_code == "BACKEND_IDENTITY_DRIFT"
                        else "BACKEND_START_FAILED"
                        if proven
                        else "BACKEND_START_UNPROVEN"
                    )
                await self._persist(
                    spec,
                    status=status,
                    terminal_reason=terminal_reason,
                    started_at=None,
                    stop_proof=start_proof,
                    error=start_error,
                    error_code=selected_error_code,
                )
                return

            started_at = _now()
            self._transition(spec, "RUNNING", "BACKEND_HANDLE_PUBLISHED")
            resource_event = self.resource_guard.event_for(spec.session_id)
            if self.stop_new.is_set() or resource_event is not None:
                reason = (
                    resource_event.code
                    if resource_event is not None
                    else "COHORT_STOP"
                )
                proof = await self._stop(handle, reason)
                if not proof.stopped:
                    self.unsafe = True
                    self.stop_new.set()
                if resource_event is None:
                    status = (
                        SessionStatus.CANCELLED
                        if proof.stopped
                        else SessionStatus.UNKNOWN
                    )
                    terminal_reason = (
                        "COHORT_CANCELLED" if proof.stopped else "STOP_UNPROVEN"
                    )
                else:
                    self._observe_resource_event(resource_event)
                    status, terminal_reason = self._resource_status(
                        resource_event, proof
                    )
                await self._persist(
                    spec,
                    status=status,
                    terminal_reason=terminal_reason,
                    started_at=started_at,
                    stop_proof=proof,
                    error_code=(
                        None if resource_event is None else resource_event.code
                    ),
                )
                return

            wait_task = asyncio.create_task(handle.wait())
            stop_task = asyncio.create_task(self.stop_new.wait())
            resource_task = asyncio.create_task(
                self.resource_guard.wait(spec.session_id)
            )
            if self.limits.session_wall_seconds is not None:
                wall_task = asyncio.create_task(
                    asyncio.sleep(float(self.limits.session_wall_seconds))
                )
            watched: set[asyncio.Task[object]] = {  # type: ignore[assignment]
                wait_task,
                stop_task,
                resource_task,
            }
            if wall_task is not None:
                watched.add(wall_task)
            done, _pending = await asyncio.wait(
                watched, return_when=asyncio.FIRST_COMPLETED
            )

            # Completion wins only if it was in the arbiter's original done
            # set.  Once stop is sent, a late success cannot change status.
            if wait_task in done:
                try:
                    outcome = wait_task.result()
                except BaseException as error:
                    proof = await self._stop(handle, "WAIT_FAILED")
                    if not proof.stopped:
                        self.unsafe = True
                        self.stop_new.set()
                    await self._persist(
                        spec,
                        status=(
                            SessionStatus.FAILED
                            if proof.stopped
                            else SessionStatus.UNKNOWN
                        ),
                        terminal_reason=(
                            "BACKEND_WAIT_FAILED"
                            if proof.stopped
                            else "STOP_UNPROVEN"
                        ),
                        started_at=started_at,
                        stop_proof=proof,
                        error=error,
                        error_code="BACKEND_WAIT_FAILED",
                    )
                else:
                    proof = await self._stop(handle, "POST_COMPLETION_CONFIRMATION")
                    if not proof.stopped:
                        self.unsafe = True
                        self.stop_new.set()
                        await self._persist(
                            spec,
                            status=SessionStatus.UNKNOWN,
                            terminal_reason="STOP_UNPROVEN",
                            started_at=started_at,
                            outcome=outcome,
                            stop_proof=proof,
                        )
                    else:
                        resource_event = await self.resource_guard.finish(
                            spec.session_id
                        )
                        self._observe_resource_event(resource_event)
                        if resource_event is not None:
                            status, terminal_reason = self._resource_status(
                                resource_event, proof
                            )
                            await self._persist(
                                spec,
                                status=status,
                                terminal_reason=terminal_reason,
                                started_at=started_at,
                                outcome=outcome,
                                stop_proof=proof,
                                error_code=resource_event.code,
                            )
                        elif not outcome.success:
                            await self._persist(
                                spec,
                                status=SessionStatus.FAILED,
                                terminal_reason=outcome.terminal_reason,
                                started_at=started_at,
                                outcome=outcome,
                                stop_proof=proof,
                            )
                        else:
                            try:
                                publications = await self._publish(spec, outcome)
                            except BaseException as error:
                                await self._persist(
                                    spec,
                                    status=SessionStatus.FAILED,
                                    terminal_reason="PMW_PUBLISH_FAILED",
                                    started_at=started_at,
                                    outcome=outcome,
                                    stop_proof=proof,
                                    publications=getattr(error, "publications", ()),
                                    error=error,
                                    error_code=_error_code(error, "PMW_PUBLISH_FAILED"),
                                )
                            else:
                                await self._persist(
                                    spec,
                                    status=SessionStatus.SUCCEEDED,
                                    terminal_reason=outcome.terminal_reason,
                                    started_at=started_at,
                                    outcome=outcome,
                                    stop_proof=proof,
                                    publications=publications,
                                )
            else:
                resource_event = (
                    resource_task.result()
                    if resource_task is not None and resource_task in done
                    else None
                )
                self._observe_resource_event(resource_event)
                wall = wall_task is not None and wall_task in done
                reason = (
                    resource_event.code
                    if resource_event is not None
                    else "SESSION_WALL_LIMIT"
                    if wall
                    else "COHORT_STOP"
                )
                self._transition(spec, "STOPPING", reason)
                proof = await self._stop(handle, reason)
                wait_task.cancel()
                await asyncio.gather(wait_task, return_exceptions=True)
                stopped_outcome: BackendOutcome | None = None
                if proof.stopped:
                    try:
                        selected_outcome = await asyncio.wait_for(
                            handle.wait(),
                            timeout=float(self.limits.stop_grace_seconds),
                        )
                        if isinstance(selected_outcome, BackendOutcome):
                            stopped_outcome = selected_outcome
                    except BaseException:
                        # Host terminal classification below remains
                        # authoritative.  A missing backend outcome merely
                        # leaves its optional evidence unavailable.
                        stopped_outcome = None
                if not proof.stopped:
                    self.unsafe = True
                    self.stop_new.set()
                if resource_event is not None:
                    status, terminal_reason = self._resource_status(
                        resource_event, proof
                    )
                    error_code = resource_event.code
                else:
                    status = (
                        SessionStatus.FAILED
                        if wall and proof.stopped
                        else SessionStatus.CANCELLED
                        if proof.stopped
                        else SessionStatus.UNKNOWN
                    )
                    terminal_reason = reason if proof.stopped else "STOP_UNPROVEN"
                    error_code = reason if wall else None
                await self._persist(
                    spec,
                    status=status,
                    terminal_reason=terminal_reason,
                    started_at=started_at,
                    outcome=stopped_outcome,
                    stop_proof=proof,
                    error_code=error_code,
                )

            for task in (stop_task, wall_task, resource_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (stop_task, wall_task, resource_task)
                    if task is not None
                ),
                return_exceptions=True,
            )
        except BaseException as error:
            proof = None
            if handle is not None:
                proof = await self._stop(handle, "HOST_FAILURE")
            proven = handle is None or (proof is not None and proof.stopped)
            if not proven:
                self.unsafe = True
                self.stop_new.set()
            await self._persist(
                spec,
                status=SessionStatus.FAILED if proven else SessionStatus.UNKNOWN,
                terminal_reason=(
                    "HOST_SESSION_FAILED" if proven else "STOP_UNPROVEN"
                ),
                started_at=started_at,
                stop_proof=proof,
                error=error,
                error_code="HOST_SESSION_FAILED",
            )
        finally:
            owned = tuple(
                task
                for task in (wait_task, stop_task, wall_task, resource_task)
                if task is not None
            )
            for task in owned:
                if not task.done():
                    task.cancel()
            if owned:
                await asyncio.gather(*owned, return_exceptions=True)

    async def _persist_not_started(self, spec: SessionSpec) -> None:
        await self._persist(
            spec,
            status=SessionStatus.CANCELLED,
            terminal_reason="NOT_STARTED",
            started_at=None,
            stop_proof=StopProof(stopped=True, reason="NOT_STARTED"),
        )

    async def worker(self) -> None:
        while True:
            spec = await self._next()
            if spec is None:
                return
            await self.run_one(spec)

    async def settle_missing(self) -> None:
        for spec in self.prepared.plan.sessions:
            if spec.session_id not in self.receipts:
                await self._persist_not_started(spec)

    def settlement_value(self) -> dict[str, object]:
        rows = []
        for spec in self.prepared.plan.sessions:
            receipt = self.receipts.get(spec.session_id)
            digest = self.receipt_sha256.get(spec.session_id)
            if receipt is None or digest is None:
                continue
            rows.append({
                "session_id": spec.session_id,
                "status": receipt["status"],
                "receipt_sha256": digest,
            })
        counts = Counter(str(row["status"]) for row in rows)
        if self.store_failed or len(rows) != len(self.prepared.plan.sessions):
            outcome = "SETTLEMENT_INCOMPLETE"
        elif self.unsafe or counts[SessionStatus.UNKNOWN.value]:
            outcome = "UNSAFE"
        elif counts[SessionStatus.CANCELLED.value]:
            outcome = "CANCELLED"
        elif counts[SessionStatus.FAILED.value]:
            outcome = "COMPLETED_WITH_FAILURES"
        else:
            outcome = "SUCCEEDED"
        return {
            "schema": RUNTIME_SETTLEMENT_SCHEMA,
            "launch_sha256": self.launch_sha256,
            "plan_sha256": self.prepared.plan.sha256,
            "cohort_id": self.prepared.plan.cohort_id,
            "finished_at": _now(),
            "outcome": outcome,
            "counts": {
                status.value: counts[status.value] for status in SessionStatus
            },
            "receipts": rows,
        }


async def run_prepared_cohort(
    prepared: PreparedCohort,
    backend: RuntimeBackend,
    *,
    limits: RuntimeLimits | None = None,
    context_policy: ContextWindowPolicy | None = None,
    publisher: ContributionPublisher | None = None,
    required_checkers: Sequence[RequiredReadinessChecker] = (),
    verifier_kit: VerifierKit | None = None,
    agenda_arm: AgendaArm | None = None,
) -> RuntimeRunResult:
    """Run exactly the sessions frozen in one authenticated plan bundle."""

    selected_limits = RuntimeLimits() if limits is None else limits
    selected_context = (
        ContextWindowPolicy() if context_policy is None else context_policy
    )
    if not isinstance(prepared, PreparedCohort):
        raise TypeError("prepared must be PreparedCohort")
    if not isinstance(selected_limits, RuntimeLimits):
        raise TypeError("limits must be RuntimeLimits")
    if not isinstance(selected_context, ContextWindowPolicy):
        raise TypeError("context_policy must be ContextWindowPolicy")
    if isinstance(required_checkers, (str, bytes)) or not isinstance(
        required_checkers, Sequence
    ):
        raise TypeError("required_checkers must be a sequence")
    if verifier_kit is not None and not isinstance(verifier_kit, VerifierKit):
        raise TypeError("verifier_kit must be VerifierKit")
    if agenda_arm is not None:
        for name in (
            "launch_value",
            "briefing_announcement",
            "review",
            "observe",
            "settle",
            "session_evidence",
        ):
            if not callable(getattr(agenda_arm, name, None)):
                raise TypeError(f"agenda_arm must implement {name}()")
        # An arm that does not cover every planned session would raise while
        # a receipt is being built, i.e. after the launch exists and with no
        # settlement to fall back on.  Prove coverage here instead, where the
        # rejection is still read-only.
        for spec in prepared.plan.sessions:
            try:
                agenda_arm.session_evidence(spec)
            except Exception as error:
                raise RuntimeOrchestrationError(
                    "AGENDA_ARM_SESSION_SET_MISMATCH", spec.session_id
                ) from error
    identity = backend.identity
    if not isinstance(identity, BackendIdentity):
        raise TypeError("backend.identity must be BackendIdentity")
    context_window_control = getattr(
        backend,
        "context_window_control",
        ContextWindowControl.NOT_APPLICABLE,
    )
    if not isinstance(context_window_control, ContextWindowControl):
        raise TypeError(
            "backend.context_window_control must be ContextWindowControl"
        )
    # Bind before creating RuntimeClaim/runtime files so a stale session ID or
    # unsupported treatment remains a genuinely read-only launch rejection.
    selected_context.bind(
        [spec.session_id for spec in prepared.plan.sessions]
    )
    if (
        selected_context.configured
        and context_window_control
        is not ContextWindowControl.NATIVE_MODEL_WINDOW
    ):
        raise RuntimeOrchestrationError("CONTEXT_WINDOW_CONTROL_UNSUPPORTED")
    context_validator = getattr(backend, "validate_context_window_policy", None)
    if context_validator is not None:
        result = context_validator(
            selected_context,
            tuple(spec.session_id for spec in prepared.plan.sessions),
        )
        if inspect.isawaitable(result) or result is not None:
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise TypeError(
                "backend.validate_context_window_policy must be synchronous and return None"
            )
    publication_identity = PublicationIdentity.disabled()
    if publisher is not None:
        publication_identity = getattr(publisher, "identity", None)
        if not isinstance(publication_identity, PublicationIdentity):
            raise TypeError(
                "publisher.identity must be PublicationIdentity"
            )

    with RuntimeClaim(prepared.cohort_root):
        store = RuntimeStore(prepared.cohort_root)
        # Advisory preflight is outside the claim.  These mutable pin and
        # apparatus checks are repeated here, before any runtime path exists,
        # and their public evidence becomes immutable launch identity.
        await asyncio.to_thread(backend.verify_runtime)
        required_readiness = await asyncio.to_thread(
            verify_required_readiness,
            required_checkers,
            prepared=prepared,
            backend=backend,
            limits=selected_limits,
            context_policy=selected_context,
            publication_identity=publication_identity,
        )
        launch = build_launch_manifest(
            prepared,
            identity,
            selected_limits,
            publication_identity,
            selected_context,
            context_window_control,
            required_readiness,
            verifier_kit,
            agenda_arm,
        )
        launch_sha256 = store.create_launch(
            launch,
            session_ids=[spec.session_id for spec in prepared.plan.sessions],
        )
        session_ids = tuple(
            spec.session_id for spec in prepared.plan.sessions
        )
        resource_guard = ResourceGuard(
            prepared.profile,
            store,
            session_ids,
        )
        initial_resource_event = await resource_guard.start()
        controller = _Controller(
            prepared=prepared,
            backend=backend,
            identity=identity,
            store=store,
            launch_sha256=launch_sha256,
            limits=selected_limits,
            context_policy=selected_context,
            context_window_control=context_window_control,
            publisher=publisher,
            publication_identity=publication_identity,
            required_readiness=required_readiness,
            resource_guard=resource_guard,
            verifier_kit=verifier_kit,
            agenda_arm=agenda_arm,
        )
        controller._observe_resource_event(initial_resource_event)
        workers = tuple(
            asyncio.create_task(controller.worker())
            for _ in range(prepared.plan.concurrency)
        )
        group = asyncio.gather(*workers, return_exceptions=True)
        cancelled = False
        try:
            await asyncio.shield(group)
        except asyncio.CancelledError:
            cancelled = True
            if not group.done():
                controller.external_cancel = True
                controller.stop_new.set()
            # Durable cleanup is shielded from repeated caller cancellation.
            while not group.done():
                try:
                    await asyncio.shield(group)
                except asyncio.CancelledError:
                    controller.external_cancel = True
                    controller.stop_new.set()

        worker_errors = [
            item for item in group.result() if isinstance(item, BaseException)
        ]
        if worker_errors:
            controller.unsafe = True
            controller.stop_new.set()
        async def finish_settlement() -> tuple[dict[str, object], str]:
            try:
                await controller.settle_missing()
            finally:
                await resource_guard.close()
            selected = controller.settlement_value()
            if selected["outcome"] == "SETTLEMENT_INCOMPLETE":
                raise RuntimeOrchestrationError("SETTLEMENT_INCOMPLETE")
            try:
                digest = store.write_settlement(selected)
            except RuntimeStoreError as error:
                raise RuntimeOrchestrationError(
                    "SETTLEMENT_INCOMPLETE"
                ) from error
            return selected, digest

        finish_task = asyncio.create_task(finish_settlement())
        while True:
            try:
                settlement, settlement_sha256 = await asyncio.shield(finish_task)
                break
            except asyncio.CancelledError:
                cancelled = True
                # All backend workers are already terminal.  Keep their
                # classifications and only shield the durable index write.
                continue
        result = RuntimeRunResult(
            launch_sha256=launch_sha256,
            settlement_sha256=settlement_sha256,
            outcome=str(settlement["outcome"]),
            receipts=tuple(
                controller.receipts[spec.session_id]
                for spec in prepared.plan.sessions
            ),
            settlement=settlement,
        )
        if cancelled:
            raise asyncio.CancelledError()
        return result


async def run_runtime_cohort(
    data_root: str | Path,
    cohort_id: str,
    backend: RuntimeBackend,
    *,
    limits: RuntimeLimits | None = None,
    context_policy: ContextWindowPolicy | None = None,
    publisher: ContributionPublisher | None = None,
    required_checkers: Sequence[RequiredReadinessChecker] = (),
    verifier_kit: VerifierKit | None = None,
    agenda_arm: AgendaArm | None = None,
    profiles_dir: str | Path | None = None,
    core_lock_path: str | Path | None = None,
) -> RuntimeRunResult:
    """Authenticate a saved bundle, then run it without caller-made specs."""

    prepared = authenticate_plan_bundle(
        data_root,
        cohort_id,
        profiles_dir=profiles_dir,
        core_lock_path=core_lock_path,
    )
    return await run_prepared_cohort(
        prepared,
        backend,
        limits=limits,
        context_policy=context_policy,
        publisher=publisher,
        required_checkers=required_checkers,
        verifier_kit=verifier_kit,
        agenda_arm=agenda_arm,
    )
