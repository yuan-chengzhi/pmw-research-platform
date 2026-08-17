"""C-arm central coordination: directives and the citation rule.

A directive is valid only from a session listed in
``roles.coordinator_session_ids``.  That list is frozen in the cohort plan
before the launch, so coordination authority is an experimental treatment
assigned by the host, not a status the agents can negotiate at run time.

Liveness is snapshot-local and needs no mutable flag: a directive is live until
a later valid coordinator directive supersedes it.
"""

from __future__ import annotations

from typing import Mapping

from ...world.records import ResearchContribution
from .candidates import parse_candidate, require_contribution
from .schemas import (
    CITED_DIRECTIVE_REFS_FIELD,
    DIRECTIVE_SCHEMA,
    PRIMARY_ACTION_KINDS,
    TREATMENT_KIND_BINDING,
    AgendaSchemaError,
    DirectivePayload,
    cited_directive_refs,
    payload_schema,
)
from .snapshot import AgendaRoles, AgendaSnapshot
from .verdict import Verdict, accept, reject


def directives(
    snapshot: AgendaSnapshot,
    roles: AgendaRoles,
) -> Mapping[str, DirectivePayload]:
    """Return every valid coordinator directive keyed by admission ref."""

    return {
        entry.admission_ref: payload
        for entry, payload in snapshot.typed(
            DIRECTIVE_SCHEMA,
            DirectivePayload.parse,
            allowed_kinds=TREATMENT_KIND_BINDING[DIRECTIVE_SCHEMA],
        )
        if roles.is_coordinator(entry.session_id)
    }


def superseded_directive_refs(
    snapshot: AgendaSnapshot,
    roles: AgendaRoles,
) -> frozenset[str]:
    """Return directive refs that a later valid directive has superseded."""

    valid = directives(snapshot, roles)
    superseded: set[str] = set()
    for admission_ref, payload in valid.items():
        for reference in payload.supersedes_refs:
            if reference != admission_ref and reference in valid:
                superseded.add(reference)
    return frozenset(superseded)


def live_directive_refs(
    snapshot: AgendaSnapshot,
    roles: AgendaRoles,
) -> tuple[str, ...]:
    """Return the directive refs still in force at this snapshot."""

    superseded = superseded_directive_refs(snapshot, roles)
    return tuple(
        sorted(
            reference
            for reference in directives(snapshot, roles)
            if reference not in superseded
        )
    )


def validate_directive(
    snapshot: AgendaSnapshot,
    candidate: object,
    *,
    roles: AgendaRoles,
    prospective_session_id: str | None = None,
) -> Verdict:
    """Accept a directive only from a designated coordinator slot."""

    payload, rejection = parse_candidate(
        candidate, schema=DIRECTIVE_SCHEMA, parser=DirectivePayload.parse
    )
    if rejection is not None:
        return rejection
    assert isinstance(payload, DirectivePayload)
    if prospective_session_id is None:
        return reject("AUTHOR_IDENTITY_REQUIRED", "directive")
    if not roles.is_coordinator(prospective_session_id):
        return reject("NOT_A_COORDINATOR_SLOT", prospective_session_id)
    valid = directives(snapshot, roles)
    for reference in payload.supersedes_refs:
        if reference not in valid:
            return reject("SUPERSEDED_DIRECTIVE_UNKNOWN", reference)
    return accept()


def validate_directive_citation(
    snapshot: AgendaSnapshot,
    candidate: object,
    *,
    roles: AgendaRoles,
) -> Verdict:
    """Require a primary action record to cite at least one live directive.

    Scope of the rule:

    * ``ATTEMPT``, ``RESULT`` and ``CHECKPOINT`` are primary actions and must
      cite.  ``NOTE``, ``NEED`` and ``OBJECTION`` are not: a treatment that
      could suppress an objection for want of a directive would corrupt the
      very evidence the experiment collects.
    * A directive record is itself exempt.  It is the source of the authority
      being cited, not an action taken under one.
    * Citation uses the ``cited_directive_refs`` payload field, never
      ``parent_refs``, which stays free for mathematical lineage.

    A record may cite several directives, including superseded ones as
    lineage; the rule is satisfied when at least one cited directive is live.
    """

    rejection = require_contribution(candidate)
    if rejection is not None:
        return rejection
    assert isinstance(candidate, ResearchContribution)
    if candidate.kind not in PRIMARY_ACTION_KINDS:
        return accept(f"{candidate.kind} is not a primary action record")
    payload = candidate.payload
    if payload_schema(payload) == DIRECTIVE_SCHEMA:
        return accept("a directive does not cite itself")
    try:
        references = cited_directive_refs(payload)
    except AgendaSchemaError as error:
        return reject("PAYLOAD_MALFORMED", error.detail)
    if references is None:
        return reject("DIRECTIVE_CITATION_MISSING", f"no {CITED_DIRECTIVE_REFS_FIELD}")
    if not references:
        return reject("DIRECTIVE_CITATION_MISSING", "empty citation list")
    valid = directives(snapshot, roles)
    for reference in references:
        if reference not in valid:
            return reject("DIRECTIVE_UNKNOWN", reference)
    live = set(live_directive_refs(snapshot, roles))
    if not live.intersection(references):
        return reject("DIRECTIVE_NOT_LIVE", references[0])
    return accept()
