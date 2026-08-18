"""Agenda arms as **toolsets**, wired into the host publication path.

An arm is a set of instruments a cohort may use, not a regime the host imposes.
Nothing here latches, hardens, schedules or steers research: the arm decides
only *which record shapes this launch admits*, and stamps the decision.  The
mechanical hardening trigger is deliberately absent from this module; it lives
in :mod:`pmw_platform.experiments.agenda_observables` as an analysis-time
observable, never in a control path.

The four arms
-------------
``P``
    ``{advisory}``.  The six contribution kinds as today.  A typed
    agenda-treatment payload is out of arm and is rejected at publication with
    :data:`OUT_OF_ARM_INSTRUMENT`.
``D``
    ``{binding}``.  The worklist instruments are validated and admitted, with
    **open admission**: any session may submit a ``TaskProposal`` at any time,
    there is no initializer role and no quiescence gate.  Plain advisory speech
    stays legal -- D constrains action-claiming, not speech.
``A``
    ``{advisory, binding}``.  Both instrument families are legal and nothing is
    required; which instrument fits a route is the agent's decision.
``C``
    ``{advisory, directive}``.  A ``Directive`` is valid only from a configured
    coordinator slot.  The citation validator is available and off by default.

What "enforcement" means here, exactly
--------------------------------------
This module validates and stamps.  It does not police research behaviour: a
session under D that never claims anything is not corrected, and
``require_claim_for_primary_action`` is recorded for a future enforcement layer
rather than applied.  A rejected instrument is a **normal research event**,
recorded in the session's settlement evidence; it never fails a session and
never degrades a settlement.

The agenda clock
----------------
Tick = the world's admission counter; a record's tick is its admission index.
The host holds that ledger: the launch's opening tick is the number of
admissions already in the world, and each admission this launch publishes
advances the counter by one.  Admissions that predate the launch are given the
opening tick, an *upper bound* on their true index, so a lease inherited from an
earlier lifetime never looks older than it is and therefore never expires early.
Nothing in the clock is authored by a research process.

Lease lifecycle
---------------
A holder's leases release when its session settles.  The lease holder is the
bound ``session_id``, and a settled session can never write again, so its claims
stop occupying their tasks at that moment.  Sessions outside this launch are
already dead by construction -- session IDs are cohort-scoped and never reused
-- so they hold nothing.  TTL in admission ticks remains and guards within-life
squatting only; the stalled-world caveat (a world with no activity never
advances its clock) is accepted and documented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any, Mapping, NoReturn, Sequence, TYPE_CHECKING

from ..world.records import ResearchContribution, canonical_json
from .agenda_treatments import (
    DECOMPOSITION_SCHEMA,
    DIRECTIVE_SCHEMA,
    PRIMARY_ACTION_KINDS,
    ROUTE_DECLARATION_SCHEMA,
    TASK_ADMISSION_SCHEMA,
    TASK_CLAIM_SCHEMA,
    TASK_OUTCOME_SCHEMA,
    TASK_PROPOSAL_SCHEMA,
    TASK_RELEASE_SCHEMA,
    TREATMENT_KIND_BINDING,
    VERDICT_CODES,
    AgendaRoles,
    AgendaSnapshot,
    RouteDeclarationPayload,
    TaskClaimPayload,
    Verdict,
    accept,
    blocking_claim_refs,
    payload_schema,
    validate_decomposition,
    validate_directive,
    validate_directive_citation,
    validate_route_declaration,
    validate_task_admission,
    validate_task_claim,
    validate_task_outcome,
    validate_task_proposal,
    validate_task_release,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..sessions.model import SessionSpec


AGENDA_ARM_LAUNCH_SCHEMA = "PMW_AGENDA_ARM_LAUNCH_1"
AGENDA_ARM_ANNOUNCEMENT_SCHEMA = "PMW_AGENDA_ARM_1"
AGENDA_ARM_EVIDENCE_SCHEMA = "PMW_AGENDA_ARM_SESSION_EVIDENCE_1"

ARM_MODE_ENFORCED = "ENFORCED"
ARM_MODE_NOT_CONFIGURED = "NOT_CONFIGURED"
ARM_MODES = frozenset({ARM_MODE_ENFORCED, ARM_MODE_NOT_CONFIGURED})

#: The one arm-level rejection.  It is not a plugin verdict: the instrument was
#: never evaluated, because this launch does not expose it at all.
OUT_OF_ARM_INSTRUMENT = "OUT_OF_ARM_INSTRUMENT"
ARM_VERDICT_CODES = frozenset(VERDICT_CODES | {OUT_OF_ARM_INSTRUMENT})

ADVISORY = "advisory"
BINDING = "binding"
DIRECTIVE = "directive"
INSTRUMENTS = (ADVISORY, BINDING, DIRECTIVE)

ARMS = ("P", "D", "A", "C")
ARM_INSTRUMENTS: Mapping[str, tuple[str, ...]] = {
    "P": (ADVISORY,),
    "D": (BINDING,),
    "A": (ADVISORY, BINDING),
    "C": (ADVISORY, DIRECTIVE),
}

# The ``advisory`` instrument is the six contribution kinds exactly as they
# already exist, so it names no typed payload schema of its own.
INSTRUMENT_SCHEMAS: Mapping[str, frozenset[str]] = {
    ADVISORY: frozenset(),
    BINDING: frozenset({
        TASK_PROPOSAL_SCHEMA,
        TASK_ADMISSION_SCHEMA,
        TASK_CLAIM_SCHEMA,
        TASK_RELEASE_SCHEMA,
        TASK_OUTCOME_SCHEMA,
        DECOMPOSITION_SCHEMA,
    }),
    DIRECTIVE: frozenset({DIRECTIVE_SCHEMA}),
}

#: Telemetry belongs to no instrument family and is legal under every arm.  If
#: route measurement varied with the treatment, the instrument that measures
#: routes would itself become part of the treatment.
TELEMETRY_SCHEMAS = frozenset({ROUTE_DECLARATION_SCHEMA})

# Enforced at import: every treatment payload the plugin knows about is
# classified as an instrument or as telemetry.  An unclassified schema would
# silently become out-of-arm everywhere; failing here is louder and earlier.
_CLASSIFIED = frozenset().union(*INSTRUMENT_SCHEMAS.values()) | TELEMETRY_SCHEMAS
_UNCLASSIFIED = frozenset(TREATMENT_KIND_BINDING) - _CLASSIFIED
if _UNCLASSIFIED:
    raise AssertionError(
        f"agenda-treatment schemas with no instrument family: {sorted(_UNCLASSIFIED)!r}"
    )
del _CLASSIFIED, _UNCLASSIFIED

ALL_SESSIONS = "ALL_SESSIONS"

AGENDA_CLOCK_SEMANTICS = "WORLD_ADMISSION_COUNTER_TICK_1"
LEASE_RELEASE_AUTHORITY = "HOST_SESSION_SETTLEMENT_RELEASES_HOLDER_LEASES_1"
ARM_ENFORCEMENT_SCOPE = (
    "PUBLICATION_TIME_RECORD_VALIDATION_ONLY_NO_RESEARCH_BEHAVIOUR_POLICING"
)
REJECTION_SEMANTICS = (
    "REJECTED_INSTRUMENT_IS_A_RESEARCH_EVENT_NOT_A_SESSION_FAILURE"
)

MAXIMUM_RECORDED_DECISIONS = 64
MAXIMUM_DETAIL_BYTES = 512
MAXIMUM_RELEASED_CLAIM_REFS = 64
MAXIMUM_TICK = (1 << 63) - 1

_CLAIM_LIVENESS_REJECTIONS = frozenset(
    {"TASK_CLAIM_CONFLICT", "LEASE_LIVENESS_UNDECIDABLE"}
)


class AgendaArmError(ValueError):
    """A launch-time agenda-arm configuration is unusable."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _fail(code: str, detail: str = "") -> NoReturn:
    raise AgendaArmError(code, detail)


def _session_ids(values: object, *, label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail("MALFORMED_AGENDA_ARM", label)
    selected: list[str] = []
    for value in values:
        if type(value) is not str or not value:
            _fail("MALFORMED_AGENDA_ARM", label)
        selected.append(value)
    if len(selected) != len(set(selected)):
        _fail("MALFORMED_AGENDA_ARM", f"{label}: duplicate")
    return tuple(sorted(selected))


def _instrument_of(schema: str | None) -> str | None:
    """Return the instrument family a payload schema belongs to.

    A plain record and a telemetry record are both ``advisory``: neither takes
    authority.  ``None`` means an unclassified treatment schema, which every
    arm refuses.
    """

    if schema is None or schema in TELEMETRY_SCHEMAS:
        return ADVISORY
    for instrument in (BINDING, DIRECTIVE):
        if schema in INSTRUMENT_SCHEMAS[instrument]:
            return instrument
    return None


@dataclass(frozen=True, slots=True)
class AgendaArmConfig:
    """The frozen instrument exposure for one launch.

    Slots are session IDs decided before the launch, so agenda authority is an
    assigned treatment rather than something the agents negotiate at run time.
    ``admitting_slots`` may be the sentinel :data:`ALL_SESSIONS`, which is what
    the D arm's open admission means: no initializer role, no quiescence gate,
    every session able to put its own private signal on the worklist.
    """

    arm: str
    coordinator_session_ids: tuple[str, ...] = ()
    admitting_slots: str | tuple[str, ...] = ALL_SESSIONS
    require_claim_for_primary_action: bool = False
    enforce_directive_citation: bool = False

    def __post_init__(self) -> None:
        if type(self.arm) is not str or self.arm not in ARM_INSTRUMENTS:
            _fail("UNKNOWN_AGENDA_ARM", str(self.arm)[:64])
        object.__setattr__(
            self,
            "coordinator_session_ids",
            _session_ids(
                self.coordinator_session_ids, label="coordinator_session_ids"
            ),
        )
        if self.admitting_slots != ALL_SESSIONS:
            object.__setattr__(
                self,
                "admitting_slots",
                _session_ids(self.admitting_slots, label="admitting_slots"),
            )
        for label in (
            "require_claim_for_primary_action",
            "enforce_directive_citation",
        ):
            if type(getattr(self, label)) is not bool:
                _fail("MALFORMED_AGENDA_ARM", label)
        if self.enforce_directive_citation and DIRECTIVE not in self.instruments:
            # Requiring a citation to an instrument the arm does not expose
            # would silence every primary action in the cohort.
            _fail("MALFORMED_AGENDA_ARM", "enforce_directive_citation")

    @property
    def instruments(self) -> tuple[str, ...]:
        return ARM_INSTRUMENTS[self.arm]

    @property
    def admitted_payload_schemas(self) -> frozenset[str]:
        """Return the typed payloads this arm admits, telemetry included."""

        selected: set[str] = set(TELEMETRY_SCHEMAS)
        for instrument in self.instruments:
            selected.update(INSTRUMENT_SCHEMAS[instrument])
        return frozenset(selected)

    @property
    def open_admission(self) -> bool:
        return self.admitting_slots == ALL_SESSIONS

    def roles(
        self,
        session_ids: Sequence[str],
        *,
        world_session_ids: Sequence[str] = (),
    ) -> AgendaRoles:
        """Resolve the configured slots against this launch's session set.

        Coordination authority is always assigned to a slot of *this* launch: a
        directive from a dead session could never be superseded.  Admission
        authority under open admission deliberately reaches further, covering
        every session that already wrote into this world, because a worklist
        that forgot the previous lifetime's tasks at each cohort boundary would
        not be a worklist.
        """

        selected = _session_ids(session_ids, label="session_ids")
        if not selected:
            _fail("MALFORMED_AGENDA_ARM", "session_ids")
        for coordinator in self.coordinator_session_ids:
            if coordinator not in selected:
                _fail("AGENDA_SLOT_NOT_IN_COHORT", coordinator)
        if self.admitting_slots == ALL_SESSIONS:
            admitting = tuple(
                sorted(
                    set(selected)
                    | set(_session_ids(world_session_ids, label="world_session_ids"))
                )
            )
        else:
            for admitter in self.admitting_slots:
                if admitter not in selected:
                    _fail("AGENDA_SLOT_NOT_IN_COHORT", admitter)
            admitting = tuple(self.admitting_slots)
        return AgendaRoles(
            coordinator_session_ids=self.coordinator_session_ids,
            admitting_session_ids=admitting,
        )

    def launch_value(self) -> dict[str, object]:
        """Return the bounded arm identity frozen into ``launch.json``."""

        return {
            "schema": AGENDA_ARM_LAUNCH_SCHEMA,
            "mode": ARM_MODE_ENFORCED,
            "arm": self.arm,
            "instruments": list(self.instruments),
            "admitted_payload_schemas": sorted(self.admitted_payload_schemas),
            "coordinator_session_ids": list(self.coordinator_session_ids),
            "admitting_slots": (
                ALL_SESSIONS
                if self.open_admission
                else list(self.admitting_slots)
            ),
            "open_admission": self.open_admission,
            "require_claim_for_primary_action": (
                self.require_claim_for_primary_action
            ),
            "enforce_directive_citation": self.enforce_directive_citation,
            "agenda_clock": AGENDA_CLOCK_SEMANTICS,
            "lease_release": LEASE_RELEASE_AUTHORITY,
            "enforcement": ARM_ENFORCEMENT_SCOPE,
            "rejection_semantics": REJECTION_SEMANTICS,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.launch_value())).hexdigest()


def not_configured_agenda_arm_launch_value() -> dict[str, object]:
    """Return the exact launch block used when no arm is configured."""

    return {
        "schema": AGENDA_ARM_LAUNCH_SCHEMA,
        "mode": ARM_MODE_NOT_CONFIGURED,
        "reason": "NO_AGENDA_ARM_CONFIGURED",
        "enforcement": ARM_ENFORCEMENT_SCOPE,
    }


def absent_agenda_arm_announcement() -> dict[str, object]:
    """Announce, on the same prompt surface, that this launch exposes no arm."""

    return {
        "schema": AGENDA_ARM_ANNOUNCEMENT_SCHEMA,
        "configured": False,
        "statements": [
            "This launch configures no agenda arm.",
            "Records are published as written, with no instrument validation.",
        ],
    }


def _evidence_value(
    *,
    arm: str,
    arm_sha256: str,
    reviewed: int,
    admitted: int,
    rejected: int,
    verdicts: Mapping[str, int],
    instrument_attempts: Mapping[str, int],
    records_by_schema: Mapping[str, int],
    route: Mapping[str, int],
    released_claim_refs: Sequence[str],
    released_at_tick: int | None,
    base_tick: int | None,
    settled_tick: int | None,
    decisions: Sequence[Mapping[str, object]],
    truncated: bool,
    divergences: int,
) -> dict[str, object]:
    """Assemble one session's enforced-arm settlement evidence.

    Two counts mean slightly less than their names suggest, so both are said
    plainly here rather than guessed at by a reader:

    * ``instrument_attempts`` counts every candidate that *reached for* an
      instrument, admitted or not.  A refused directive under the A arm is
      still evidence that a session wanted one; subtract the verdict counts to
      get admissions only.
    * ``publication_divergences`` is cohort-wide, not per session.  A publisher
      that admits bytes the arm never validated corrupts the shared ledger, so
      every session settling after it inherits the warning.
    """

    return {
        "schema": AGENDA_ARM_EVIDENCE_SCHEMA,
        "mode": ARM_MODE_ENFORCED,
        "arm": arm,
        "arm_sha256": arm_sha256,
        "reviewed": reviewed,
        "admitted": admitted,
        "rejected": rejected,
        "verdicts": dict(verdicts),
        "instrument_attempts": {
            instrument: int(instrument_attempts.get(instrument, 0))
            for instrument in INSTRUMENTS
        },
        "records_by_schema": dict(records_by_schema),
        "route_declarations": {
            "count": int(route.get("count", 0)),
            "with_peer_trigger_refs": int(
                route.get("with_peer_trigger_refs", 0)
            ),
            "resolved_peer_trigger_refs": int(
                route.get("resolved_peer_trigger_refs", 0)
            ),
            "dangling_rejected": int(route.get("dangling_rejected", 0)),
            "differentiation_notes": int(route.get("differentiation_notes", 0)),
        },
        "lease_release": {
            "authority": LEASE_RELEASE_AUTHORITY,
            "released_claim_refs": list(released_claim_refs),
            "released_at_tick": released_at_tick,
        },
        "agenda_clock": {
            "semantics": AGENDA_CLOCK_SEMANTICS,
            "base_tick": base_tick,
            "settled_tick": settled_tick,
        },
        "decisions": [dict(item) for item in decisions],
        "truncated": truncated,
        "publication_divergences": divergences,
        "rejection_semantics": REJECTION_SEMANTICS,
    }


def not_configured_agenda_arm_session_evidence() -> dict[str, object]:
    """Return the receipt block used when a launch configured no arm.

    Deliberately not a zero-filled copy of the enforced block: a launch with no
    arm has no verdicts, no clock and no leases to report, and saying so is
    different from reporting measured zeros for all of them.  The three counts
    are real, though -- nothing was reviewed, admitted or rejected -- and they
    keep the receipt's contribution accounting closed.
    """

    return {
        "schema": AGENDA_ARM_EVIDENCE_SCHEMA,
        "mode": ARM_MODE_NOT_CONFIGURED,
        "arm": None,
        "arm_sha256": None,
        "reviewed": 0,
        "admitted": 0,
        "rejected": 0,
        "reason": "NO_AGENDA_ARM_CONFIGURED",
    }


@dataclass(frozen=True, slots=True)
class ArmDecision:
    """One immutable arm decision about one candidate contribution.

    ``code`` is drawn from :data:`ARM_VERDICT_CODES`: every plugin verdict code,
    plus the arm-level :data:`OUT_OF_ARM_INSTRUMENT`, which means the instrument
    was never evaluated because this launch does not expose it.
    """

    session_id: str
    ordinal: int
    kind: str
    payload_schema: str | None
    instrument: str | None
    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if type(self.code) is not str or self.code not in ARM_VERDICT_CODES:
            raise ValueError(f"unknown agenda-arm verdict code: {self.code!r}")
        if type(self.detail) is not str:
            raise TypeError("detail must be str")
        encoded = self.detail.encode("utf-8", errors="replace")
        if len(encoded) > MAXIMUM_DETAIL_BYTES:
            object.__setattr__(
                self,
                "detail",
                encoded[:MAXIMUM_DETAIL_BYTES].decode("utf-8", errors="ignore"),
            )

    @property
    def admitted(self) -> bool:
        return self.code == "ACCEPTED"

    def to_value(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "kind": self.kind,
            "payload_schema": self.payload_schema,
            "instrument": self.instrument,
            "code": self.code,
            "admitted": self.admitted,
            "detail": self.detail,
        }


@dataclass(slots=True)
class _SessionLedger:
    """Everything one session's settlement evidence needs, accumulated live."""

    decisions: list[ArmDecision] = field(default_factory=list)
    claim_refs: list[str] = field(default_factory=list)
    route: dict[str, int] = field(default_factory=dict)
    released_claim_refs: tuple[str, ...] = ()
    released_at_tick: int | None = None
    settled_tick: int | None = None

    def bump(self, key: str, amount: int = 1) -> None:
        self.route[key] = self.route.get(key, 0) + amount


class AgendaArm:
    """Host-side arm controller: validate at publication, stamp at settlement.

    The controller owns the two things no record can own: the agenda clock (an
    admission counter no research process can author) and the set of sessions
    that can still act (launch identity, not world content).  Every other
    decision is delegated unchanged to the pure plugin validators.
    """

    def __init__(
        self,
        config: AgendaArmConfig,
        *,
        session_ids: Sequence[str],
        base_records: Sequence[tuple[str, object]] = (),
    ) -> None:
        if not isinstance(config, AgendaArmConfig):
            raise TypeError("config must be AgendaArmConfig")
        self.config = config
        self._session_ids = frozenset(
            _session_ids(session_ids, label="session_ids")
        )
        self._entries: list[tuple[str, object]] = []
        self._ticks: dict[str, int] = {}
        seen: set[str] = set()
        if isinstance(base_records, (str, bytes)) or not isinstance(
            base_records, Sequence
        ):
            _fail("MALFORMED_AGENDA_ARM", "base_records")
        for item in base_records:
            if not isinstance(item, tuple) or len(item) != 2:
                _fail("MALFORMED_AGENDA_ARM", "base_records")
            admission_ref, content = item
            if type(admission_ref) is not str or not admission_ref:
                _fail("MALFORMED_AGENDA_ARM", "base_records ref")
            if admission_ref in seen:
                _fail("MALFORMED_AGENDA_ARM", f"duplicate {admission_ref}")
            seen.add(admission_ref)
            self._entries.append((admission_ref, content))
        self._base_tick = len(self._entries)
        # Admissions that predate this launch get the opening tick: an upper
        # bound on their true index, so an inherited lease never expires early.
        for admission_ref, _content in self._entries:
            self._ticks[admission_ref] = self._base_tick
        self._now_tick = self._base_tick
        self._sessions: dict[str, _SessionLedger] = {}
        self._settled: set[str] = set()
        self._divergences = 0
        self._snapshot: AgendaSnapshot | None = None
        self.roles = config.roles(
            session_ids,
            world_session_ids=sorted(
                {
                    entry.session_id
                    for entry in self.snapshot().entries
                    if entry.session_id is not None
                }
                - self._session_ids
            ),
        )
        self._launch_value = config.launch_value()
        self._launch_sha256 = hashlib.sha256(
            canonical_json(self._launch_value)
        ).hexdigest()

    # -- identity ----------------------------------------------------------

    @property
    def arm(self) -> str:
        return self.config.arm

    @property
    def base_tick(self) -> int:
        return self._base_tick

    @property
    def now_tick(self) -> int:
        return self._now_tick

    @property
    def sha256(self) -> str:
        return self._launch_sha256

    def launch_value(self) -> dict[str, object]:
        return dict(self._launch_value)

    def admissions(self) -> tuple[tuple[str, object], ...]:
        """Return this launch's admission-ordered ledger, for later analysis."""

        return tuple(self._entries)

    def settled_session_ids(self) -> frozenset[str]:
        return frozenset(self._settled)

    # -- briefing surface --------------------------------------------------

    def briefing_announcement(self) -> dict[str, object]:
        """Announce the arm's instruments without prescribing a research route.

        The wording states which record shapes are legal, how claiming and
        leases behave, and what the host does with a rejection.  It contains no
        route advice, no ordering and no success criterion, because the
        experiment measures which instrument a session chooses, not whether it
        can follow one the host recommended.
        """

        return {
            "schema": AGENDA_ARM_ANNOUNCEMENT_SCHEMA,
            "configured": True,
            "arm": self.config.arm,
            "instruments": list(self.config.instruments),
            "admitted_payload_schemas": sorted(
                self.config.admitted_payload_schemas
            ),
            "record_shapes": [
                {
                    "payload_schema": schema,
                    "rides_kinds": sorted(TREATMENT_KIND_BINDING[schema]),
                    "instrument": _instrument_of(schema),
                }
                for schema in sorted(self.config.admitted_payload_schemas)
            ],
            "claiming": self._announced_claiming(),
            "route_declaration": {
                "payload_schema": ROUTE_DECLARATION_SCHEMA,
                "rides_kind": "ATTEMPT",
                "fields": [
                    "route_statement",
                    "peer_trigger_refs",
                    "differentiation_note",
                ],
                "legal_under_every_arm": True,
                "peer_trigger_refs": (
                    "admission refs, which the briefing already exposes; a "
                    "reference that does not resolve is rejected, not stored"
                ),
            },
            "statements": self._announced_statements(),
        }

    def _announced_claiming(self) -> dict[str, object]:
        if BINDING not in self.config.instruments:
            return {
                "available": False,
                "reason": "this arm exposes no binding worklist instrument",
            }
        return {
            "available": True,
            "sequence": [
                "TaskProposal: any session, at any time",
                "TaskAdmission: an admitting slot puts the task on the worklist",
                "TaskClaim: an exclusive lease on one admitted task",
                "TaskRelease or TaskOutcome: the holder closes its own lease",
            ],
            "open_admission": self.config.open_admission,
            "admitting_slots": (
                ALL_SESSIONS
                if self.config.open_admission
                else list(self.config.admitting_slots)
            ),
            "task_identity": "the admission ref of the task's TaskAdmission",
            "exclusivity": "at most one live claim per task",
            "lease_ticks": (
                "a duration counted in world admissions; the start tick is host "
                "evidence and cannot be written into a record"
            ),
            "release": [
                "the holder closes its own lease with TaskRelease or TaskOutcome",
                "a holder's leases release when its session settles",
                "a lease also expires once lease_ticks admissions have elapsed",
            ],
        }

    def _announced_statements(self) -> list[str]:
        statements = [
            "This record announces which instruments this launch admits; it "
            "recommends no route, no ordering and no success criterion.",
            "A record whose payload names an instrument outside this arm is "
            "rejected at publication and recorded as OUT_OF_ARM_INSTRUMENT.",
            "A rejected instrument is a recorded research event, not a session "
            "failure; the session settles normally either way.",
            "Plain records on the six contribution kinds stay legal under "
            "every arm.",
        ]
        if BINDING in self.config.instruments:
            statements.append(
                "This arm's worklist is the instrument through which action "
                "on a route is claimed; the host records whether it was used "
                "and does not otherwise police research behaviour."
                if self.config.require_claim_for_primary_action
                else "Proposing, admitting and claiming are available; using "
                "any of them is optional, and using none of them is a "
                "permitted outcome."
            )
        if DIRECTIVE in self.config.instruments:
            statements.append(
                "A Directive is valid only from a configured coordinator "
                "slot; a directive written by any other session is rejected."
            )
            statements.append(
                "Primary action records must cite a live directive."
                if self.config.enforce_directive_citation
                else "Citing a directive is available and not required."
            )
        statements.append(
            "Records written by a session become visible to other sessions "
            "only in a later briefing, after this session has settled."
        )
        return statements

    # -- publication-time review -------------------------------------------

    def snapshot(self) -> AgendaSnapshot:
        """Project the world as this host has observed it, plus its clock."""

        if self._snapshot is None:
            self._snapshot = AgendaSnapshot.build(
                list(self._entries),
                now_tick=self._now_tick,
                observed_at_ticks=dict(self._ticks),
            )
        return self._snapshot

    def review(self, spec: "SessionSpec", contribution: object) -> ArmDecision:
        """Decide one candidate against the arm and the then-current snapshot."""

        session_id = self._session_id(spec)
        ledger = self._ledger(session_id)
        ordinal = len(ledger.decisions) + 1
        if not isinstance(contribution, ResearchContribution):
            return self._record(
                ledger,
                ArmDecision(
                    session_id=session_id,
                    ordinal=ordinal,
                    kind="",
                    payload_schema=None,
                    instrument=None,
                    code="CANDIDATE_NOT_IDENTITY_FREE",
                    detail=type(contribution).__name__,
                ),
            )
        schema = payload_schema(contribution.payload)
        instrument = _instrument_of(schema)
        if (
            schema is not None
            and schema not in self.config.admitted_payload_schemas
        ):
            return self._record(
                ledger,
                ArmDecision(
                    session_id=session_id,
                    ordinal=ordinal,
                    kind=contribution.kind,
                    payload_schema=schema,
                    instrument=instrument,
                    code=OUT_OF_ARM_INSTRUMENT,
                    detail=(
                        f"arm {self.config.arm} exposes "
                        f"{list(self.config.instruments)!r}"
                    ),
                ),
            )
        verdict = self._verdict(session_id, contribution, schema)
        decision = self._record(
            ledger,
            ArmDecision(
                session_id=session_id,
                ordinal=ordinal,
                kind=contribution.kind,
                payload_schema=schema,
                instrument=instrument,
                code=verdict.code,
                detail=verdict.detail,
            ),
        )
        if schema == ROUTE_DECLARATION_SCHEMA:
            self._count_route_declaration(ledger, contribution, decision)
        return decision

    @staticmethod
    def _record(ledger: _SessionLedger, decision: ArmDecision) -> ArmDecision:
        ledger.decisions.append(decision)
        return decision

    def _verdict(
        self,
        session_id: str,
        contribution: ResearchContribution,
        schema: str | None,
    ) -> Verdict:
        snapshot = self.snapshot()
        if schema is None:
            if (
                self.config.enforce_directive_citation
                and contribution.kind in PRIMARY_ACTION_KINDS
            ):
                return validate_directive_citation(
                    snapshot, contribution, roles=self.roles
                )
            return accept()
        if schema == ROUTE_DECLARATION_SCHEMA:
            return validate_route_declaration(snapshot, contribution)
        if schema == TASK_PROPOSAL_SCHEMA:
            # Open admission: any session, at any time, with no initializer
            # role and no quiescence gate.
            return validate_task_proposal(snapshot, contribution, roles=self.roles)
        if schema == TASK_ADMISSION_SCHEMA:
            return validate_task_admission(
                snapshot,
                contribution,
                roles=self.roles,
                prospective_session_id=session_id,
            )
        if schema == TASK_CLAIM_SCHEMA:
            return self._claim_verdict(snapshot, contribution)
        if schema == TASK_RELEASE_SCHEMA:
            return validate_task_release(
                snapshot,
                contribution,
                roles=self.roles,
                prospective_session_id=session_id,
            )
        if schema == TASK_OUTCOME_SCHEMA:
            return validate_task_outcome(
                snapshot,
                contribution,
                roles=self.roles,
                prospective_session_id=session_id,
            )
        if schema == DIRECTIVE_SCHEMA:
            return validate_directive(
                snapshot,
                contribution,
                roles=self.roles,
                prospective_session_id=session_id,
            )
        if schema == DECOMPOSITION_SCHEMA:
            return validate_decomposition(snapshot, contribution, roles=self.roles)
        raise AssertionError(f"admitted schema without a validator: {schema!r}")

    def _claim_verdict(
        self,
        snapshot: AgendaSnapshot,
        contribution: ResearchContribution,
    ) -> Verdict:
        """Apply the plugin's exclusivity rule, then the host's release rule.

        The plugin decides liveness from world content alone, so it treats every
        unclosed claim as occupying its task.  Only the host knows which holders
        can still act.  This override never tightens the plugin's answer; it
        vacates a conflict exactly when every blocking claim is held by a
        session that has already settled, or by a session outside this launch,
        which is dead by construction.
        """

        verdict = validate_task_claim(snapshot, contribution, roles=self.roles)
        if verdict.code not in _CLAIM_LIVENESS_REJECTIONS:
            return verdict
        try:
            payload = TaskClaimPayload.parse(contribution.payload)
        except ValueError:  # pragma: no cover - already parsed by the validator
            return verdict
        blocking = blocking_claim_refs(snapshot, payload.task_ref, self.roles)
        if not blocking:  # pragma: no cover - a rejection implies a blocker
            return verdict
        for reference in blocking:
            entry = snapshot.get(reference)
            if entry is None or entry.session_id is None:
                return verdict
            if not self._holder_is_released(entry.session_id):
                return verdict
        return accept(
            f"leases released at holder settlement: {list(blocking)!r}"
        )

    def _holder_is_released(self, holder_session_id: str) -> bool:
        if holder_session_id not in self._session_ids:
            # A session outside this launch can never write again: session IDs
            # are cohort-scoped and are never reused.
            return True
        return holder_session_id in self._settled

    def _count_route_declaration(
        self,
        ledger: _SessionLedger,
        contribution: ResearchContribution,
        decision: ArmDecision,
    ) -> None:
        ledger.bump("count")
        try:
            payload = RouteDeclarationPayload.parse(contribution.payload)
        except ValueError:
            return
        if payload.peer_trigger_refs:
            ledger.bump("with_peer_trigger_refs")
        if payload.differentiation_note is not None:
            ledger.bump("differentiation_notes")
        if decision.code == "ROUTE_TRIGGER_REF_UNKNOWN":
            ledger.bump("dangling_rejected")
        elif decision.admitted:
            ledger.bump(
                "resolved_peer_trigger_refs", len(payload.peer_trigger_refs)
            )

    def observe(
        self,
        spec: "SessionSpec",
        contribution: object,
        result: object,
    ) -> None:
        """Record one durable admission and advance the world's tick counter.

        The publish result's content digest is checked against the record the
        arm reviewed.  A mismatch means the publisher admitted something other
        than what was validated; that admission is then kept in the ledger as an
        opaque reference -- it can satisfy no treatment rule -- and counted as a
        divergence in the settlement evidence rather than silently trusted.
        """

        session_id = self._session_id(spec)
        admission_ref, content_sha256 = _publication_identity(result)
        if admission_ref is None:
            self._divergences += 1
            return
        content: object = None
        if isinstance(contribution, ResearchContribution):
            bound = _bound_record(contribution, spec)
            if bound is not None and bound[1] == content_sha256:
                content = bound[0]
        if content is None:
            self._divergences += 1
        self._append(admission_ref, content)
        if (
            isinstance(contribution, ResearchContribution)
            and payload_schema(contribution.payload) == TASK_CLAIM_SCHEMA
        ):
            self._ledger(session_id).claim_refs.append(admission_ref)

    def _append(self, admission_ref: str, content: object) -> None:
        if admission_ref in self._ticks:
            self._divergences += 1
            return
        if self._now_tick >= MAXIMUM_TICK:  # pragma: no cover - unreachable
            _fail("AGENDA_CLOCK_EXHAUSTED", admission_ref)
        self._now_tick += 1
        self._ticks[admission_ref] = self._now_tick
        self._entries.append((admission_ref, content))
        self._snapshot = None

    # -- settlement --------------------------------------------------------

    def settle(self, spec: "SessionSpec") -> tuple[str, ...]:
        """Release the settling holder's leases and freeze its clock reading.

        A settled session can never write again, so from this tick onward its
        claims stop occupying their tasks.  The released refs are recorded here
        rather than reconstructed later, because the release is a host runtime
        event with no representation in world content.
        """

        session_id = self._session_id(spec)
        ledger = self._ledger(session_id)
        if session_id in self._settled:
            return ledger.released_claim_refs
        snapshot = self.snapshot()
        released = tuple(
            sorted(
                reference
                for reference in ledger.claim_refs
                if _claim_occupies(snapshot, reference, self.roles)
            )
        )[:MAXIMUM_RELEASED_CLAIM_REFS]
        self._settled.add(session_id)
        ledger.released_claim_refs = released
        ledger.released_at_tick = self._now_tick
        ledger.settled_tick = self._now_tick
        return released

    def session_evidence(self, spec: "SessionSpec") -> dict[str, object]:
        """Return this session's settlement evidence for its durable receipt."""

        ledger = self._ledger(self._session_id(spec))
        verdicts: dict[str, int] = {}
        instruments: dict[str, int] = {}
        by_schema: dict[str, int] = {}
        admitted = 0
        for decision in ledger.decisions:
            verdicts[decision.code] = verdicts.get(decision.code, 0) + 1
            if decision.instrument is not None:
                instruments[decision.instrument] = (
                    instruments.get(decision.instrument, 0) + 1
                )
            if decision.payload_schema is not None:
                by_schema[decision.payload_schema] = (
                    by_schema.get(decision.payload_schema, 0) + 1
                )
            if decision.admitted:
                admitted += 1
        recorded = ledger.decisions[:MAXIMUM_RECORDED_DECISIONS]
        return _evidence_value(
            arm=self.config.arm,
            arm_sha256=self._launch_sha256,
            reviewed=len(ledger.decisions),
            admitted=admitted,
            rejected=len(ledger.decisions) - admitted,
            verdicts=verdicts,
            instrument_attempts=instruments,
            records_by_schema=by_schema,
            route=ledger.route,
            released_claim_refs=ledger.released_claim_refs,
            released_at_tick=ledger.released_at_tick,
            base_tick=self._base_tick,
            settled_tick=(
                self._now_tick
                if ledger.settled_tick is None
                else ledger.settled_tick
            ),
            decisions=[item.to_value() for item in recorded],
            truncated=len(ledger.decisions) > len(recorded),
            divergences=self._divergences,
        )

    def _session_id(self, spec: object) -> str:
        session_id = getattr(spec, "session_id", None)
        if type(session_id) is not str or session_id not in self._session_ids:
            _fail("SESSION_NOT_IN_LAUNCH", str(session_id)[:128])
        return session_id

    def _ledger(self, session_id: str) -> _SessionLedger:
        ledger = self._sessions.get(session_id)
        if ledger is None:
            ledger = _SessionLedger()
            self._sessions[session_id] = ledger
        return ledger


def build_agenda_arm(
    config: AgendaArmConfig,
    *,
    session_ids: Sequence[str],
    world: object | None = None,
    snapshot_ref: str | None = None,
) -> AgendaArm:
    """Open an arm over a world's current admissions.

    Reading the world once, at launch, is deliberate: the opening tick is the
    world's admission count at that moment, and every later tick comes from this
    host's own serialized publications.
    """

    base_records: tuple[tuple[str, object], ...] = ()
    if world is not None:
        rows = world.records(snapshot_ref)  # type: ignore[attr-defined]
        base_records = tuple((row.admission_ref, row.content) for row in rows)
    return AgendaArm(
        config, session_ids=session_ids, base_records=base_records
    )


def _claim_occupies(
    snapshot: AgendaSnapshot,
    claim_ref: str,
    roles: AgendaRoles,
) -> bool:
    entry = snapshot.get(claim_ref)
    if entry is None:
        return False
    try:
        parsed = TaskClaimPayload.parse(entry.payload)
    except ValueError:
        return False
    return claim_ref in blocking_claim_refs(snapshot, parsed.task_ref, roles)


def _publication_identity(result: object) -> tuple[str | None, str | None]:
    value: Any = result
    if hasattr(value, "to_value") and callable(value.to_value):
        value = value.to_value()
    if type(value) is not dict:
        return None, None
    admission_ref = value.get("admission_ref")
    content_sha256 = value.get("content_sha256")
    if type(admission_ref) is not str or not admission_ref:
        return None, None
    return admission_ref, (
        content_sha256 if type(content_sha256) is str else None
    )


def _bound_record(
    contribution: ResearchContribution,
    spec: object,
) -> tuple[dict[str, object], str] | None:
    try:
        record = contribution.bind(spec)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return record.to_value(), record.content_sha256
