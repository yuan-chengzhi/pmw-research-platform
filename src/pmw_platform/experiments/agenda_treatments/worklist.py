"""D-arm worklist: exclusive task leases with a TTL, over an exact snapshot.

Every function here is pure.  Given the same snapshot value, the same roles and
the same candidate, the verdict is the same; nothing reads a clock, a file, an
environment variable or a model.

The lease algebra
-----------------
A *task* is the admission ref of a valid ``TaskAdmission`` written by a session
in ``roles.admitting_session_ids``.  A task admitted by an unauthorized slot is
not on the worklist at all, so a session cannot self-admit work and then claim
it.

A claim ``C`` on task ``X`` is in exactly one state at a snapshot:

``CLOSED``
    Its holder published a matching ``TaskRelease`` or ``TaskOutcome``.  Only
    the holder can close its own lease, and closure outranks expiry.
``EXPIRED``
    ``now_tick >= observed_at_tick(C) + lease_ticks``.
``LIVE``
    Not closed and not expired.
``UNDECIDABLE``
    Not closed, and the host supplied no clock for this claim.  The plugin
    refuses to guess, and every rule treats an undecidable claim as *possibly*
    live, so no second lease is ever granted on its strength.

``blocking_claim_refs`` returns the claims that are ``LIVE`` or
``UNDECIDABLE``.  The invariant "at most one live claim per task at any
snapshot" is enforced forward by :func:`validate_task_claim` and auditable
after the fact by :func:`check_lease_exclusivity`.
"""

from __future__ import annotations

from typing import Mapping

from ...world.records import ResearchContribution
from .candidates import parse_candidate
from .schemas import (
    ARTIFACT_BACKED_CONTRACT_KINDS,
    OUTCOME_DISPOSITION_KIND,
    TASK_ADMISSION_SCHEMA,
    TASK_CLAIM_SCHEMA,
    TASK_OUTCOME_SCHEMA,
    TASK_PROPOSAL_SCHEMA,
    TASK_RELEASE_SCHEMA,
    TREATMENT_KIND_BINDING,
    TaskAdmissionPayload,
    TaskClaimPayload,
    TaskOutcomePayload,
    TaskProposalPayload,
    TaskReleasePayload,
)
from .snapshot import AgendaEntry, AgendaRoles, AgendaSnapshot
from .verdict import Verdict, accept, reject


LIVE = "LIVE"
CLOSED = "CLOSED"
EXPIRED = "EXPIRED"
UNDECIDABLE = "UNDECIDABLE"
UNKNOWN = "UNKNOWN"

CLAIM_STATES = frozenset({LIVE, CLOSED, EXPIRED, UNDECIDABLE, UNKNOWN})

# States in which a claim still occupies its task.
BLOCKING_CLAIM_STATES = frozenset({LIVE, UNDECIDABLE})


def _typed(snapshot: AgendaSnapshot, schema: str, parser):  # type: ignore[no-untyped-def]
    return snapshot.typed(schema, parser, allowed_kinds=TREATMENT_KIND_BINDING[schema])


def admitted_tasks(
    snapshot: AgendaSnapshot,
    roles: AgendaRoles,
) -> Mapping[str, TaskAdmissionPayload]:
    """Return the worklist: authorized task admissions keyed by admission ref."""

    return {
        entry.admission_ref: payload
        for entry, payload in _typed(
            snapshot, TASK_ADMISSION_SCHEMA, TaskAdmissionPayload.parse
        )
        if roles.is_admitter(entry.session_id)
    }


def proposals(snapshot: AgendaSnapshot) -> Mapping[str, TaskProposalPayload]:
    """Return every valid task proposal; any peer may write one."""

    return {
        entry.admission_ref: payload
        for entry, payload in _typed(
            snapshot, TASK_PROPOSAL_SCHEMA, TaskProposalPayload.parse
        )
    }


def _claims(
    snapshot: AgendaSnapshot,
) -> tuple[tuple[AgendaEntry, TaskClaimPayload], ...]:
    return tuple(
        (entry, payload)
        for entry, payload in _typed(snapshot, TASK_CLAIM_SCHEMA, TaskClaimPayload.parse)
        if entry.session_id is not None
    )


def _releases(
    snapshot: AgendaSnapshot,
) -> tuple[tuple[AgendaEntry, TaskReleasePayload], ...]:
    return tuple(
        (entry, payload)
        for entry, payload in _typed(
            snapshot, TASK_RELEASE_SCHEMA, TaskReleasePayload.parse
        )
        if entry.session_id is not None
    )


def _outcomes(
    snapshot: AgendaSnapshot,
) -> tuple[tuple[AgendaEntry, TaskOutcomePayload], ...]:
    return tuple(
        (entry, payload)
        for entry, payload in _typed(
            snapshot, TASK_OUTCOME_SCHEMA, TaskOutcomePayload.parse
        )
        if entry.session_id is not None
        and entry.kind == OUTCOME_DISPOSITION_KIND[payload.disposition]
    )


def _closure_refs(snapshot: AgendaSnapshot) -> frozenset[str]:
    """Claim refs closed by their own holder via a matching release or outcome."""

    claim_by_ref = {entry.admission_ref: (entry, payload) for entry, payload in _claims(snapshot)}
    closed: set[str] = set()
    for entry, payload in _releases(snapshot) + _outcomes(snapshot):  # type: ignore[operator]
        selected = claim_by_ref.get(payload.claim_ref)
        if selected is None:
            continue
        claim_entry, claim_payload = selected
        if (
            claim_entry.session_id == entry.session_id
            and claim_payload.task_ref == payload.task_ref
        ):
            closed.add(payload.claim_ref)
    return frozenset(closed)


def claim_state(snapshot: AgendaSnapshot, claim_ref: str) -> str:
    """Return the lease state of one claim ref at this snapshot."""

    for entry, payload in _claims(snapshot):
        if entry.admission_ref != claim_ref:
            continue
        if claim_ref in _closure_refs(snapshot):
            return CLOSED
        if snapshot.now_tick is None or entry.observed_at_tick is None:
            return UNDECIDABLE
        if snapshot.now_tick >= entry.observed_at_tick + payload.lease_ticks:
            return EXPIRED
        return LIVE
    return UNKNOWN


def blocking_claim_refs(
    snapshot: AgendaSnapshot,
    task_ref: str,
    roles: AgendaRoles,
) -> tuple[str, ...]:
    """Return claims that still occupy ``task_ref`` (``LIVE`` or ``UNDECIDABLE``)."""

    if task_ref not in admitted_tasks(snapshot, roles):
        return ()
    closed = _closure_refs(snapshot)
    selected: list[str] = []
    for entry, payload in _claims(snapshot):
        if payload.task_ref != task_ref or entry.admission_ref in closed:
            continue
        if snapshot.now_tick is None or entry.observed_at_tick is None:
            selected.append(entry.admission_ref)
        elif snapshot.now_tick < entry.observed_at_tick + payload.lease_ticks:
            selected.append(entry.admission_ref)
    return tuple(sorted(selected))


def task_is_completed(
    snapshot: AgendaSnapshot,
    task_ref: str,
    roles: AgendaRoles,
) -> bool:
    """Return whether a completed outcome with intact chain of custody exists.

    Chain of custody means the ``COMPLETED`` outcome names a claim on the same
    task, and that claim was held by the very session that wrote the outcome.
    A peer cannot declare another session's task complete.
    """

    if task_ref not in admitted_tasks(snapshot, roles):
        return False
    claim_by_ref = {
        entry.admission_ref: (entry, payload) for entry, payload in _claims(snapshot)
    }
    for entry, payload in _outcomes(snapshot):
        if payload.task_ref != task_ref or payload.disposition != "COMPLETED":
            continue
        selected = claim_by_ref.get(payload.claim_ref)
        if selected is None:
            continue
        claim_entry, claim_payload = selected
        if (
            claim_entry.session_id == entry.session_id
            and claim_payload.task_ref == task_ref
        ):
            return True
    return False


def check_lease_exclusivity(
    snapshot: AgendaSnapshot,
    roles: AgendaRoles,
) -> Verdict:
    """Audit the whole snapshot for the one-live-lease-per-task invariant.

    Undecidable claims count as occupying their task, so a snapshot whose host
    supplied no agenda clock cannot be certified exclusive.  That is deliberate:
    without a clock the invariant is unproven, not satisfied.
    """

    for task_ref in sorted(admitted_tasks(snapshot, roles)):
        blocking = blocking_claim_refs(snapshot, task_ref, roles)
        if len(blocking) > 1:
            return reject(
                "TASK_CLAIM_CONFLICT",
                f"{task_ref} occupied by {list(blocking)!r}",
            )
    return accept()


def _dependency_verdict(
    snapshot: AgendaSnapshot,
    dependency_refs: tuple[str, ...],
    roles: AgendaRoles,
) -> Verdict | None:
    tasks = admitted_tasks(snapshot, roles)
    for reference in dependency_refs:
        if reference not in tasks:
            return reject("TASK_DEPENDENCY_UNKNOWN", reference)
    for reference in dependency_refs:
        if not task_is_completed(snapshot, reference, roles):
            return reject("TASK_DEPENDENCIES_UNREADY", reference)
    return None


def validate_task_proposal(
    snapshot: AgendaSnapshot,
    candidate: object,
    *,
    roles: AgendaRoles,
) -> Verdict:
    """Any peer may propose work; declared dependencies must already exist."""

    payload, rejection = parse_candidate(
        candidate, schema=TASK_PROPOSAL_SCHEMA, parser=TaskProposalPayload.parse
    )
    if rejection is not None:
        return rejection
    assert isinstance(payload, TaskProposalPayload)
    tasks = admitted_tasks(snapshot, roles)
    for reference in payload.dependency_task_refs:
        if reference not in tasks:
            return reject("TASK_DEPENDENCY_UNKNOWN", reference)
    return accept()


def validate_task_admission(
    snapshot: AgendaSnapshot,
    candidate: object,
    *,
    roles: AgendaRoles,
    prospective_session_id: str | None = None,
) -> Verdict:
    """Only a designated admitting slot may put a task on the worklist.

    ``prospective_session_id`` is the identity the host would inject at
    ``bind``.  It is a host-supplied argument, never read from the payload.
    """

    payload, rejection = parse_candidate(
        candidate, schema=TASK_ADMISSION_SCHEMA, parser=TaskAdmissionPayload.parse
    )
    if rejection is not None:
        return rejection
    assert isinstance(payload, TaskAdmissionPayload)
    if prospective_session_id is None:
        return reject("AUTHOR_IDENTITY_REQUIRED", "task admission")
    if not roles.is_admitter(prospective_session_id):
        return reject("NOT_AN_ADMITTING_SLOT", prospective_session_id)
    if payload.proposal_ref is not None and payload.proposal_ref not in proposals(
        snapshot
    ):
        return reject("PROPOSAL_UNKNOWN", payload.proposal_ref)
    tasks = admitted_tasks(snapshot, roles)
    for reference in payload.dependency_task_refs:
        if reference not in tasks:
            return reject("TASK_DEPENDENCY_UNKNOWN", reference)
    return accept()


def validate_task_claim(
    snapshot: AgendaSnapshot,
    candidate: object,
    *,
    roles: AgendaRoles,
) -> Verdict:
    """Grant an exclusive lease only when the task is free and ready.

    This validator deliberately needs no author identity.  Exclusivity is a
    property of the world, not of who is asking: if any claim still occupies
    the task, the next claim is refused whoever writes it, including the
    current holder.  Renewal is therefore an explicit release-then-claim.
    """

    payload, rejection = parse_candidate(
        candidate, schema=TASK_CLAIM_SCHEMA, parser=TaskClaimPayload.parse
    )
    if rejection is not None:
        return rejection
    assert isinstance(payload, TaskClaimPayload)
    tasks = admitted_tasks(snapshot, roles)
    task = tasks.get(payload.task_ref)
    if task is None:
        return reject("TASK_UNKNOWN", payload.task_ref)
    if task_is_completed(snapshot, payload.task_ref, roles):
        return reject("TASK_ALREADY_COMPLETED", payload.task_ref)
    dependency_rejection = _dependency_verdict(
        snapshot, task.dependency_task_refs, roles
    )
    if dependency_rejection is not None:
        return dependency_rejection
    blocking = blocking_claim_refs(snapshot, payload.task_ref, roles)
    if blocking:
        undecidable = [
            reference
            for reference in blocking
            if claim_state(snapshot, reference) == UNDECIDABLE
        ]
        if len(undecidable) == len(blocking):
            return reject("LEASE_LIVENESS_UNDECIDABLE", blocking[0])
        return reject("TASK_CLAIM_CONFLICT", blocking[0])
    return accept()


def _held_claim(
    snapshot: AgendaSnapshot,
    *,
    task_ref: str,
    claim_ref: str,
    roles: AgendaRoles,
    prospective_session_id: str | None,
) -> tuple[str, Verdict | None]:
    """Resolve the claim a release/outcome closes, or return the rejection."""

    if task_ref not in admitted_tasks(snapshot, roles):
        return "", reject("TASK_UNKNOWN", task_ref)
    selected: tuple[AgendaEntry, TaskClaimPayload] | None = None
    for entry, payload in _claims(snapshot):
        if entry.admission_ref == claim_ref:
            selected = (entry, payload)
            break
    if selected is None:
        return "", reject("CLAIM_UNKNOWN", claim_ref)
    claim_entry, claim_payload = selected
    if claim_payload.task_ref != task_ref:
        return "", reject(
            "CLAIM_TASK_MISMATCH", f"{claim_ref} holds {claim_payload.task_ref}"
        )
    if prospective_session_id is None:
        return "", reject("AUTHOR_IDENTITY_REQUIRED", claim_ref)
    if claim_entry.session_id != prospective_session_id:
        return "", reject("CLAIM_NOT_HELD_BY_AUTHOR", claim_ref)
    return claim_state(snapshot, claim_ref), None


def validate_task_release(
    snapshot: AgendaSnapshot,
    candidate: object,
    *,
    roles: AgendaRoles,
    prospective_session_id: str | None = None,
) -> Verdict:
    """Close a lease without claiming an outcome.

    An expired lease may still be released: recording that the holder walked
    away is bookkeeping, not an authority claim.  A lease already closed cannot
    be closed twice.
    """

    payload, rejection = parse_candidate(
        candidate, schema=TASK_RELEASE_SCHEMA, parser=TaskReleasePayload.parse
    )
    if rejection is not None:
        return rejection
    assert isinstance(payload, TaskReleasePayload)
    state, held_rejection = _held_claim(
        snapshot,
        task_ref=payload.task_ref,
        claim_ref=payload.claim_ref,
        roles=roles,
        prospective_session_id=prospective_session_id,
    )
    if held_rejection is not None:
        return held_rejection
    if state == CLOSED:
        return reject("CLAIM_ALREADY_CLOSED", payload.claim_ref)
    return accept()


def validate_task_outcome(
    snapshot: AgendaSnapshot,
    candidate: object,
    *,
    roles: AgendaRoles,
    prospective_session_id: str | None = None,
) -> Verdict:
    """Close a lease with a disposition checked against the admitted contract.

    The contract check is a *shape and provenance* check.  The plugin verifies
    that the offered evidence names the contract the task was admitted under
    and, for artifact-backed contracts, that the record actually carries an
    artifact.  It never re-runs a verifier and never asserts the mathematics is
    correct; authoritative verification remains the host's post-settlement path.
    """

    payload, rejection = parse_candidate(
        candidate, schema=TASK_OUTCOME_SCHEMA, parser=TaskOutcomePayload.parse
    )
    if rejection is not None:
        return rejection
    assert isinstance(payload, TaskOutcomePayload)
    assert isinstance(candidate, ResearchContribution)
    required_kind = OUTCOME_DISPOSITION_KIND[payload.disposition]
    if candidate.kind != required_kind:
        return reject(
            "RECORD_KIND_NOT_ALLOWED",
            f"{payload.disposition} outcome must ride {required_kind}",
        )
    state, held_rejection = _held_claim(
        snapshot,
        task_ref=payload.task_ref,
        claim_ref=payload.claim_ref,
        roles=roles,
        prospective_session_id=prospective_session_id,
    )
    if held_rejection is not None:
        return held_rejection
    if state == CLOSED:
        return reject("CLAIM_ALREADY_CLOSED", payload.claim_ref)
    if state == EXPIRED:
        return reject("LEASE_EXPIRED", payload.claim_ref)
    if state == UNDECIDABLE:
        return reject("LEASE_LIVENESS_UNDECIDABLE", payload.claim_ref)
    if payload.disposition == "COMPLETED":
        task = admitted_tasks(snapshot, roles)[payload.task_ref]
        evidence = payload.completion_evidence
        assert evidence is not None  # guaranteed by TaskOutcomePayload.parse
        if evidence.contract_kind != task.completion_contract.contract_kind:
            return reject(
                "COMPLETION_CONTRACT_MISMATCH",
                f"task requires {task.completion_contract.contract_kind}",
            )
        if (
            evidence.contract_kind in ARTIFACT_BACKED_CONTRACT_KINDS
            and not candidate.artifact_refs
        ):
            return reject(
                "COMPLETION_EVIDENCE_MISSING",
                f"{evidence.contract_kind} requires an artifact ref",
            )
    return accept()
