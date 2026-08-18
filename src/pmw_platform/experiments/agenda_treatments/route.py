"""Route telemetry: a typed route declaration with resolvable peer citations.

A route declaration is deliberately *not* an instrument.  It confers no lease,
supersedes nothing and blocks nobody, so it is legal under every agenda arm.
That is load-bearing for the experiment: if route measurement were available
only under some arms, the measuring apparatus would itself vary with the
treatment and the route comparison would be uninterpretable.

The one rule this module enforces is fail-closed citation.  A declaration may
name the peer admissions that moved it onto its route; every named reference
must resolve inside the snapshot the host validates against.  A dangling
reference is rejected rather than stored, because an unresolvable citation is
not weaker evidence -- it is no evidence, and an experiment that counted it
would be counting the agents' prose.
"""

from __future__ import annotations

from typing import Mapping

from .candidates import parse_candidate
from .schemas import (
    ROUTE_DECLARATION_SCHEMA,
    TREATMENT_KIND_BINDING,
    RouteDeclarationPayload,
)
from .snapshot import AgendaSnapshot
from .verdict import Verdict, accept, reject


def route_declarations(
    snapshot: AgendaSnapshot,
) -> Mapping[str, RouteDeclarationPayload]:
    """Return every valid route declaration keyed by admission ref."""

    return {
        entry.admission_ref: payload
        for entry, payload in snapshot.typed(
            ROUTE_DECLARATION_SCHEMA,
            RouteDeclarationPayload.parse,
            allowed_kinds=TREATMENT_KIND_BINDING[ROUTE_DECLARATION_SCHEMA],
        )
    }


def resolved_peer_trigger_refs(
    snapshot: AgendaSnapshot,
    payload: RouteDeclarationPayload,
) -> tuple[str, ...]:
    """Return the cited peer triggers that resolve inside this snapshot."""

    if not isinstance(payload, RouteDeclarationPayload):
        raise TypeError("payload must be RouteDeclarationPayload")
    return tuple(
        reference
        for reference in payload.peer_trigger_refs
        if snapshot.get(reference) is not None
    )


def validate_route_declaration(
    snapshot: AgendaSnapshot,
    candidate: object,
) -> Verdict:
    """Accept a route declaration whose peer citations all resolve.

    No role is consulted: any session may state which route it took.  The
    validator needs no author identity either, because a declaration asserts
    nothing about authority -- only about what its author read and chose.
    """

    payload, rejection = parse_candidate(
        candidate,
        schema=ROUTE_DECLARATION_SCHEMA,
        parser=RouteDeclarationPayload.parse,
    )
    if rejection is not None:
        return rejection
    assert isinstance(payload, RouteDeclarationPayload)
    for reference in payload.peer_trigger_refs:
        if snapshot.get(reference) is None:
            return reject("ROUTE_TRIGGER_REF_UNKNOWN", reference)
    return accept()
