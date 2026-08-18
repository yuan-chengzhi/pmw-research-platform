"""Analysis-time observables over a world's admission sequence.

Nothing in this module is reachable from a session, a briefing or the
publication path.  That separation is the point: v0.3 reclassified the
mechanical hardening trigger **from a control to an observable**, so the trigger
must be computable after the fact and must not exist anywhere a run could
consult it.  The prediction it serves -- that endogenous taskification onset
co-occurs with the trigger firing -- is falsifiable only if neither caused the
other.

Time is the world's admission counter, the same clock the arm uses: a sample's
``tick`` is the number of admissions in the world at that point, and a record's
tick is its admission index.  Admissions that predate the observed window are
collapsed into the opening sample, because their individual indices are not
recoverable from a snapshot.

One honest property, stated rather than smoothed away: the trigger is
**non-monotone**.  A later ``OBJECTION`` naming a decomposition flips a fired
sample back to unfired, so the series can leave and re-enter the fired state.
The series reports what each snapshot says; it never latches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .agenda_treatments import (
    AgendaRoles,
    AgendaSnapshot,
    admitted_tasks,
    agenda_hardening_trigger,
    proposals,
    settled_decomposition_refs,
)


TRIGGER_SERIES_SCHEMA = "PMW_AGENDA_TRIGGER_SERIES_1"

SERIES_AUTHORITY = "ANALYSIS_TIME_OBSERVABLE_NEVER_A_CONTROL_INPUT"


@dataclass(frozen=True, slots=True)
class TriggerSample:
    """The trigger's reading at one point of a world's admission sequence."""

    tick: int
    admission_ref: str | None
    fired: bool
    decomposition_refs: tuple[str, ...]
    admitted_task_count: int
    proposal_count: int

    def to_value(self) -> dict[str, object]:
        return {
            "tick": self.tick,
            "admission_ref": self.admission_ref,
            "fired": self.fired,
            "decomposition_refs": list(self.decomposition_refs),
            "admitted_task_count": self.admitted_task_count,
            "proposal_count": self.proposal_count,
        }


def _sample(
    entries: Sequence[tuple[str, object]],
    *,
    tick: int,
    admission_ref: str | None,
    target_ref: str,
    roles: AgendaRoles,
) -> TriggerSample:
    snapshot = AgendaSnapshot.build(list(entries))
    refs = settled_decomposition_refs(snapshot, target_ref, roles=roles)
    return TriggerSample(
        tick=tick,
        admission_ref=admission_ref,
        fired=bool(refs),
        decomposition_refs=refs,
        admitted_task_count=len(admitted_tasks(snapshot, roles)),
        proposal_count=len(proposals(snapshot)),
    )


def trigger_time_series(
    admissions: Sequence[tuple[str, object]],
    target_ref: str,
    *,
    roles: AgendaRoles,
    base_count: int = 0,
) -> tuple[TriggerSample, ...]:
    """Evaluate the hardening trigger along one admission-ordered ledger.

    ``admissions`` is ``(admission_ref, content)`` in admission order; the first
    ``base_count`` entries are treated as already present and are collapsed into
    the opening sample.

    ``base_count`` is not optional in practice.  A world snapshot does not
    carry its own admission order, so the records that predate the observed
    window have no recoverable individual index; walking them one at a time
    would date them by an order that does not exist.  Pass
    ``base_count=arm.base_tick`` for an :class:`AgendaArm` ledger, or use
    :func:`trigger_time_series_from_arm`, which does it for you.
    """

    if isinstance(admissions, (str, bytes)) or not isinstance(
        admissions, Sequence
    ):
        raise TypeError("admissions must be a sequence of (ref, content) pairs")
    if type(base_count) is not int or isinstance(base_count, bool):
        raise TypeError("base_count must be an integer")
    if not 0 <= base_count <= len(admissions):
        raise ValueError("base_count must lie inside the admission sequence")
    if type(target_ref) is not str or not target_ref:
        raise ValueError("target_ref must be a non-empty admission ref")
    entries = list(admissions)
    series = [
        _sample(
            entries[:base_count],
            tick=base_count,
            admission_ref=None,
            target_ref=target_ref,
            roles=roles,
        )
    ]
    for index in range(base_count, len(entries)):
        series.append(
            _sample(
                entries[: index + 1],
                tick=index + 1,
                admission_ref=entries[index][0],
                target_ref=target_ref,
                roles=roles,
            )
        )
    return tuple(series)


def trigger_time_series_from_arm(
    arm: object,
    target_ref: str,
    *,
    roles: AgendaRoles | None = None,
) -> tuple[TriggerSample, ...]:
    """Replay one completed launch's ledger, skipping its unordered prefix.

    This is the intended entry point after a run: the arm knows both its own
    admission order and where the pre-launch prefix ends, and it already holds
    the resolved roles, so nothing about the reading has to be reconstructed by
    hand.  It reads a finished ledger and cannot influence one.
    """

    return trigger_time_series(
        arm.admissions(),  # type: ignore[attr-defined]
        target_ref,
        roles=arm.roles if roles is None else roles,  # type: ignore[attr-defined]
        base_count=arm.base_tick,  # type: ignore[attr-defined]
    )


def trigger_time_series_from_world(
    world: object,
    snapshot_refs: Sequence[str],
    target_ref: str,
    *,
    roles: AgendaRoles,
) -> tuple[TriggerSample, ...]:
    """Evaluate the trigger over an explicit sequence of world snapshots.

    Each sample's tick is that snapshot's admission count, so ticks agree with
    the ledger form above without needing the ledger.  The caller supplies the
    snapshot order; this function never derives one, because a snapshot ref
    alone does not carry its own history.
    """

    if isinstance(snapshot_refs, (str, bytes)) or not isinstance(
        snapshot_refs, Sequence
    ):
        raise TypeError("snapshot_refs must be a sequence")
    samples: list[TriggerSample] = []
    for snapshot_ref in snapshot_refs:
        rows = world.records(snapshot_ref)  # type: ignore[attr-defined]
        entries = [(row.admission_ref, row.content) for row in rows]
        samples.append(
            _sample(
                entries,
                tick=len(entries),
                admission_ref=None,
                target_ref=target_ref,
                roles=roles,
            )
        )
    return tuple(samples)


def trigger_onset_tick(series: Sequence[TriggerSample]) -> int | None:
    """Return the first tick at which the trigger fired, or ``None``."""

    for sample in series:
        if sample.fired:
            return sample.tick
    return None


def taskification_onset_tick(series: Sequence[TriggerSample]) -> int | None:
    """Return the first tick carrying an authorized admitted task."""

    for sample in series:
        if sample.admitted_task_count > 0:
            return sample.tick
    return None


def trigger_cooccurrence(
    series: Sequence[TriggerSample],
    *,
    target_ref: str,
) -> dict[str, object]:
    """Summarize the two onsets this observable exists to compare.

    ``taskification_lead_ticks`` is the trigger onset minus the taskification
    onset: positive when the agents taskified before the structural trigger
    fired, negative when the trigger preceded them, ``None`` when either never
    happened.  The summary states an association and no causal claim; the
    trigger never touched the run it describes.
    """

    trigger = trigger_onset_tick(series)
    taskification = taskification_onset_tick(series)
    fired_ticks = tuple(sample.tick for sample in series if sample.fired)
    return {
        "schema": TRIGGER_SERIES_SCHEMA,
        "authority": SERIES_AUTHORITY,
        "target_ref": target_ref,
        "sample_count": len(series),
        "first_tick": series[0].tick if series else None,
        "last_tick": series[-1].tick if series else None,
        "trigger_onset_tick": trigger,
        "taskification_onset_tick": taskification,
        "taskification_lead_ticks": (
            None
            if trigger is None or taskification is None
            else trigger - taskification
        ),
        "fired_tick_count": len(fired_ticks),
        "last_fired_tick": fired_ticks[-1] if fired_ticks else None,
        # A fired reading that later unfires means a peer objected to the
        # decomposition; the series records that instead of latching.
        "non_monotone": _unfired_after_firing(series),
        "final_reading": series[-1].fired if series else None,
    }


def _unfired_after_firing(series: Sequence[TriggerSample]) -> bool:
    """Return whether the reading ever went back to unfired after firing."""

    fired = False
    for sample in series:
        if sample.fired:
            fired = True
        elif fired:
            return True
    return False


def trigger_series_value(
    series: Sequence[TriggerSample],
    *,
    target_ref: str,
) -> dict[str, object]:
    """Return the full bounded series plus its summary, ready to persist."""

    value: dict[str, object] = dict(trigger_cooccurrence(series, target_ref=target_ref))
    value["samples"] = [sample.to_value() for sample in series]
    return value


def agenda_trigger_reading(
    admissions: Sequence[tuple[str, object]],
    target_ref: str,
    *,
    roles: AgendaRoles,
) -> Mapping[str, object]:
    """Return the trigger's reading at the end of one admission sequence.

    A thin convenience over :func:`agenda_hardening_trigger` for callers that
    want a single reading rather than a series, keeping the honest labelling
    attached to it.
    """

    snapshot = AgendaSnapshot.build(list(admissions))
    return {
        "schema": TRIGGER_SERIES_SCHEMA,
        "authority": SERIES_AUTHORITY,
        "target_ref": target_ref,
        "tick": len(admissions),
        "fired": agenda_hardening_trigger(snapshot, target_ref, roles=roles),
        "decomposition_refs": list(
            settled_decomposition_refs(snapshot, target_ref, roles=roles)
        ),
        "semantics": (
            "STRUCTURAL_NOT_MATHEMATICAL_AND_NON_MONOTONE_ACROSS_SNAPSHOTS"
        ),
    }
