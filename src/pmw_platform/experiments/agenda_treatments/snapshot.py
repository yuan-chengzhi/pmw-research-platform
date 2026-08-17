"""An immutable, treatment-oriented read of one exact world snapshot.

`AgendaSnapshot` is a *projection*, never a second source of truth.  It holds
the admissions visible at one snapshot ref, each decoded into the fields the
agenda validators need: the host-injected ``session_id``, the record kind, the
payload and the lineage.

Where the agenda clock comes from
---------------------------------
PMW record content is deliberately timeless -- "timestamps and runtime receipts
deliberately live outside mathematical state".  A lease TTL nevertheless needs a
clock, so the plugin takes one *from the host*, which is the authority for
runtime settlement evidence:

* ``now_tick`` is the host's agenda clock at the moment of evaluation;
* ``observed_at_ticks`` maps an admission ref to the tick at which the host
  observed that admission.

Both are integers in a unit the host chooses and must keep consistent for one
experiment.  Neither is derived from world content, and neither can be authored
by a research process.  When either is missing for a claim, the plugin reports
the lease as *undecidable* rather than guessing; validators then fail closed and
refuse to grant a second lease.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from types import MappingProxyType
from typing import Any, Callable, Mapping, NoReturn, Sequence, TYPE_CHECKING

from ...world.records import (
    RESEARCH_RECORD_SCHEMA,
    ResearchRecord,
    ResearchRecordError,
    canonical_json,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...world.store import ResearchWorld, WorldAdmission


MAXIMUM_TICK = (1 << 63) - 1


class AgendaSnapshotError(ValueError):
    """Stable error for a malformed agenda snapshot projection."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _fail(code: str, detail: str = "") -> NoReturn:
    raise AgendaSnapshotError(code, detail)


def _tick(value: object, *, label: str) -> int:
    if type(value) is not int or isinstance(value, bool) or not 0 <= value <= MAXIMUM_TICK:
        _fail("MALFORMED_AGENDA_CLOCK", label)
    return value


@dataclass(frozen=True, slots=True)
class AgendaEntry:
    """One admission as the agenda validators see it.

    ``session_id`` is present only for admissions whose content is a bound
    :class:`~pmw_platform.world.records.ResearchRecord`.  It is the identity the
    trusted host injected at ``bind``; nothing in this module ever reads an
    author identity out of a payload.
    """

    admission_ref: str
    session_id: str | None
    kind: str | None
    parent_refs: tuple[str, ...]
    problem_ids: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    observed_at_tick: int | None
    malformed: bool
    _payload_bytes: bytes | None = field(repr=False, default=None)

    @property
    def payload(self) -> dict[str, object] | None:
        """Return a detached payload copy, or ``None`` for an opaque admission."""

        if self._payload_bytes is None:
            return None
        value = json.loads(self._payload_bytes.decode("utf-8"))
        if type(value) is not dict:  # constructor invariant
            raise AssertionError("agenda entry payload is not an object")
        return value

    @property
    def is_research_record(self) -> bool:
        return self._payload_bytes is not None


def _decode(
    admission_ref: str,
    content: object,
    observed_at_tick: int | None,
) -> AgendaEntry:
    if type(content) is not dict or content.get("schema") != RESEARCH_RECORD_SCHEMA:
        return AgendaEntry(
            admission_ref=admission_ref,
            session_id=None,
            kind=None,
            parent_refs=(),
            problem_ids=(),
            artifact_refs=(),
            observed_at_tick=observed_at_tick,
            malformed=False,
            _payload_bytes=None,
        )
    try:
        record = ResearchRecord.from_value(content)
    except ResearchRecordError:
        # A record that claims the platform schema but does not validate is
        # kept visible and opaque.  It can never satisfy a treatment rule, and
        # a host can audit it via ``malformed_admission_refs``.
        return AgendaEntry(
            admission_ref=admission_ref,
            session_id=None,
            kind=None,
            parent_refs=(),
            problem_ids=(),
            artifact_refs=(),
            observed_at_tick=observed_at_tick,
            malformed=True,
            _payload_bytes=None,
        )
    return AgendaEntry(
        admission_ref=admission_ref,
        session_id=record.session_id,
        kind=record.kind,
        parent_refs=record.parent_refs,
        problem_ids=record.problem_ids,
        artifact_refs=record.artifact_refs,
        observed_at_tick=observed_at_tick,
        malformed=False,
        _payload_bytes=canonical_json(record.payload),
    )


@dataclass(frozen=True, slots=True)
class AgendaSnapshot:
    """A detached, ordered projection of one snapshot plus its agenda clock."""

    entries: tuple[AgendaEntry, ...]
    now_tick: int | None
    _index: Mapping[str, AgendaEntry] = field(repr=False)

    @classmethod
    def build(
        cls,
        entries: Sequence[tuple[str, object]],
        *,
        now_tick: int | None = None,
        observed_at_ticks: Mapping[str, int] | None = None,
    ) -> "AgendaSnapshot":
        """Project ``(admission_ref, content)`` pairs into an agenda snapshot.

        ``observed_at_ticks`` must only name admissions present in ``entries``;
        an unknown key is a host bookkeeping error and is rejected rather than
        ignored.
        """

        if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
            _fail("MALFORMED_AGENDA_SNAPSHOT", "entries")
        selected_now = None if now_tick is None else _tick(now_tick, label="now_tick")
        ticks: dict[str, int] = {}
        if observed_at_ticks is not None:
            if not isinstance(observed_at_ticks, Mapping):
                _fail("MALFORMED_AGENDA_SNAPSHOT", "observed_at_ticks")
            for reference, value in observed_at_ticks.items():
                if type(reference) is not str:
                    _fail("MALFORMED_AGENDA_SNAPSHOT", "observed_at_ticks key")
                ticks[reference] = _tick(value, label=f"observed_at_ticks[{reference}]")
        decoded: list[AgendaEntry] = []
        seen: set[str] = set()
        for item in entries:
            if not isinstance(item, tuple) or len(item) != 2:
                _fail("MALFORMED_AGENDA_SNAPSHOT", "entry shape")
            admission_ref, content = item
            if type(admission_ref) is not str or not admission_ref:
                _fail("MALFORMED_AGENDA_SNAPSHOT", "admission_ref")
            if admission_ref in seen:
                _fail("MALFORMED_AGENDA_SNAPSHOT", f"duplicate {admission_ref}")
            seen.add(admission_ref)
            decoded.append(_decode(admission_ref, content, ticks.get(admission_ref)))
        unknown = sorted(set(ticks) - seen)
        if unknown:
            _fail("MALFORMED_AGENDA_SNAPSHOT", f"unknown observed tick {unknown[0]}")
        ordered = tuple(sorted(decoded, key=lambda entry: entry.admission_ref))
        return cls(
            entries=ordered,
            now_tick=selected_now,
            _index=MappingProxyType(
                {entry.admission_ref: entry for entry in ordered}
            ),
        )

    @classmethod
    def from_world(
        cls,
        world: "ResearchWorld",
        snapshot_ref: str | None = None,
        *,
        now_tick: int | None = None,
        observed_at_ticks: Mapping[str, int] | None = None,
    ) -> "AgendaSnapshot":
        """Project an exact world snapshot without mutating or re-admitting it."""

        rows: Sequence["WorldAdmission"] = world.records(snapshot_ref)
        return cls.build(
            [(row.admission_ref, row.content) for row in rows],
            now_tick=now_tick,
            observed_at_ticks=observed_at_ticks,
        )

    def get(self, admission_ref: str) -> AgendaEntry | None:
        if type(admission_ref) is not str:
            return None
        return self._index.get(admission_ref)

    def __contains__(self, admission_ref: object) -> bool:
        return type(admission_ref) is str and admission_ref in self._index

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def malformed_admission_refs(self) -> tuple[str, ...]:
        """Admissions claiming the platform record schema that did not validate."""

        return tuple(
            entry.admission_ref for entry in self.entries if entry.malformed
        )

    def typed(
        self,
        schema: str,
        parser: Callable[[object], Any],
        *,
        allowed_kinds: frozenset[str] | None = None,
    ) -> tuple[tuple[AgendaEntry, Any], ...]:
        """Return every entry that validly carries ``schema``.

        Entries whose payload declares ``schema`` but fails to parse, or whose
        record kind falls outside ``allowed_kinds``, are silently excluded: an
        already-admitted malformed record must not be able to influence a
        treatment rule.  Use :meth:`invalid_typed` to audit what was excluded.
        """

        selected: list[tuple[AgendaEntry, Any]] = []
        for entry, parsed in self._scan(schema, parser, allowed_kinds):
            if parsed is not None:
                selected.append((entry, parsed))
        return tuple(selected)

    def invalid_typed(
        self,
        schema: str,
        parser: Callable[[object], Any],
        *,
        allowed_kinds: frozenset[str] | None = None,
    ) -> tuple[str, ...]:
        """Return refs that declare ``schema`` but were excluded as invalid."""

        return tuple(
            entry.admission_ref
            for entry, parsed in self._scan(schema, parser, allowed_kinds)
            if parsed is None
        )

    def _scan(
        self,
        schema: str,
        parser: Callable[[object], Any],
        allowed_kinds: frozenset[str] | None,
    ) -> tuple[tuple[AgendaEntry, Any], ...]:
        results: list[tuple[AgendaEntry, Any]] = []
        for entry in self.entries:
            payload = entry.payload
            if type(payload) is not dict or payload.get("schema") != schema:
                continue
            if allowed_kinds is not None and entry.kind not in allowed_kinds:
                results.append((entry, None))
                continue
            try:
                results.append((entry, parser(payload)))
            except ValueError:
                results.append((entry, None))
        return tuple(results)


def _session_ids(values: object, *, label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail("MALFORMED_AGENDA_ROLES", label)
    selected: list[str] = []
    for value in values:
        if type(value) is not str or not value:
            _fail("MALFORMED_AGENDA_ROLES", label)
        selected.append(value)
    if len(selected) != len(set(selected)):
        _fail("MALFORMED_AGENDA_ROLES", f"{label}: duplicate")
    return tuple(sorted(selected))


@dataclass(frozen=True, slots=True)
class AgendaRoles:
    """Which session slots hold treatment authority.

    Both fields default to empty, which is fail-closed: with no configured
    slot, no session can issue a directive and no session can admit a task.
    Slots are session IDs frozen in the cohort plan, so authority is decided
    before a launch, not negotiated by the agents at run time.
    """

    coordinator_session_ids: tuple[str, ...] = ()
    admitting_session_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "coordinator_session_ids",
            _session_ids(
                self.coordinator_session_ids, label="coordinator_session_ids"
            ),
        )
        object.__setattr__(
            self,
            "admitting_session_ids",
            _session_ids(self.admitting_session_ids, label="admitting_session_ids"),
        )

    def is_coordinator(self, session_id: object) -> bool:
        return type(session_id) is str and session_id in self.coordinator_session_ids

    def is_admitter(self, session_id: object) -> bool:
        return type(session_id) is str and session_id in self.admitting_session_ids

    def to_value(self) -> dict[str, object]:
        return {
            "coordinator_session_ids": list(self.coordinator_session_ids),
            "admitting_session_ids": list(self.admitting_session_ids),
        }
