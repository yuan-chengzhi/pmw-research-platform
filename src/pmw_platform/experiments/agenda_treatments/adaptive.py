"""Adaptive arm: a deterministic agenda-hardening trigger.

The adaptive arm starts with a free agenda and hardens into worklist discipline
at the moment the problem is demonstrably decomposable.  "Demonstrably" is a
structural test over one exact snapshot, evaluated by
:func:`agenda_hardening_trigger`.  There is no model call, no scoring, no
threshold on prose, and no appeal to anything outside the snapshot.

Exact semantics
---------------
``agenda_hardening_trigger(snapshot, target_ref, roles=...)`` is ``True`` **iff
a settled decomposition covering the target exists in the snapshot**, where a
decomposition record ``D`` is *settled and covering* exactly when all five hold:

1. ``D`` parses as ``PMW_AGENDA_DECOMPOSITION_1`` and rides ``RESULT``;
2. ``D.target_ref == target_ref`` and that ref resolves to an admission present
   in the snapshot;
3. ``D`` lists at least :data:`MINIMUM_SUBLEMMAS_FOR_TRIGGER` sublemmas -- a
   reduction to a single lemma changes the statement but creates no agenda to
   coordinate, which is what hardening is for;
4. **grounded**: every sublemma's ``admission_ref`` resolves to an authorized
   ``TaskAdmission`` in the snapshot whose ``statement`` is byte-equal to the
   sublemma's statement, so the parts are really on the worklist and were not
   silently reworded;
5. **unobjected**: no ``OBJECTION`` record in the snapshot names ``D``'s
   admission ref among its ``parent_refs``.

Writing a decomposition needs no special role.  The authority lives in
condition 4: the sublemmas must be tasks an *admitting* slot put on the
worklist, so no session can harden the agenda by writing one record alone.

Two properties worth stating plainly
------------------------------------
* **Non-monotone.**  PMW snapshots only grow, but condition 5 can flip a
  ``True`` back to ``False`` when a peer later objects.  The trigger is a
  predicate *on a snapshot*, not an irreversible event, so a host that acts on
  it must record the exact snapshot ref at which it fired.
* **Structural, not mathematical.**  Firing means a decomposition was written
  down, grounded in admitted tasks, and left unchallenged.  It is not evidence
  that the sublemmas actually suffice for the target.
"""

from __future__ import annotations

from .candidates import parse_candidate
from .schemas import (
    DECOMPOSITION_SCHEMA,
    TREATMENT_KIND_BINDING,
    DecompositionPayload,
)
from .snapshot import AgendaRoles, AgendaSnapshot
from .verdict import Verdict, accept, reject
from .worklist import admitted_tasks


# A decomposition into a single part restates the target; it creates no
# parallel agenda, so it does not harden one.
MINIMUM_SUBLEMMAS_FOR_TRIGGER = 2

OBJECTION_KIND = "OBJECTION"


def validate_decomposition(
    snapshot: AgendaSnapshot,
    candidate: object,
    *,
    roles: AgendaRoles,
) -> Verdict:
    """Accept a decomposition whose parts are grounded in admitted tasks."""

    payload, rejection = parse_candidate(
        candidate, schema=DECOMPOSITION_SCHEMA, parser=DecompositionPayload.parse
    )
    if rejection is not None:
        return rejection
    assert isinstance(payload, DecompositionPayload)
    if snapshot.get(payload.target_ref) is None:
        return reject("DECOMPOSITION_TARGET_UNKNOWN", payload.target_ref)
    tasks = admitted_tasks(snapshot, roles)
    for sublemma in payload.sublemmas:
        task = tasks.get(sublemma.admission_ref)
        if task is None:
            return reject("DECOMPOSITION_SUBLEMMA_UNKNOWN", sublemma.admission_ref)
        if task.statement != sublemma.statement:
            return reject(
                "DECOMPOSITION_STATEMENT_MISMATCH", sublemma.admission_ref
            )
    return accept()


def _objected_refs(snapshot: AgendaSnapshot) -> frozenset[str]:
    """Admission refs named as a parent by some OBJECTION record."""

    objected: set[str] = set()
    for entry in snapshot.entries:
        if entry.kind == OBJECTION_KIND:
            objected.update(entry.parent_refs)
    return frozenset(objected)


def settled_decomposition_refs(
    snapshot: AgendaSnapshot,
    target_ref: str,
    *,
    roles: AgendaRoles,
) -> tuple[str, ...]:
    """Return the decomposition refs that satisfy every trigger condition.

    Exposed alongside the boolean so a host records *which* record fired the
    trigger, rather than only that something did.
    """

    if type(target_ref) is not str or snapshot.get(target_ref) is None:
        return ()
    tasks = admitted_tasks(snapshot, roles)
    objected = _objected_refs(snapshot)
    selected: list[str] = []
    for entry, payload in snapshot.typed(
        DECOMPOSITION_SCHEMA,
        DecompositionPayload.parse,
        allowed_kinds=TREATMENT_KIND_BINDING[DECOMPOSITION_SCHEMA],
    ):
        if payload.target_ref != target_ref:
            continue
        if len(payload.sublemmas) < MINIMUM_SUBLEMMAS_FOR_TRIGGER:
            continue
        if entry.admission_ref in objected:
            continue
        grounded = all(
            sublemma.admission_ref in tasks
            and tasks[sublemma.admission_ref].statement == sublemma.statement
            for sublemma in payload.sublemmas
        )
        if grounded:
            selected.append(entry.admission_ref)
    return tuple(sorted(selected))


def agenda_hardening_trigger(
    snapshot: AgendaSnapshot,
    target_ref: str,
    *,
    roles: AgendaRoles,
) -> bool:
    """Return whether a settled decomposition covering ``target_ref`` exists."""

    return bool(settled_decomposition_refs(snapshot, target_ref, roles=roles))
