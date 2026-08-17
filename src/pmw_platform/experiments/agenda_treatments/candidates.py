"""The shape gate every agenda validator applies before consulting a snapshot.

A candidate is always an identity-free
:class:`~pmw_platform.world.records.ResearchContribution`.  Passing an already
bound :class:`~pmw_platform.world.records.ResearchRecord` is rejected rather
than accepted as a convenience: the treatments validate *proposals*, and a
record that already carries a host-injected identity has passed the boundary
these validators exist to police.
"""

from __future__ import annotations

from typing import Any, Callable

from ...world.records import ResearchContribution
from .schemas import TREATMENT_KIND_BINDING, AgendaSchemaError
from .verdict import VERDICT_CODES, Verdict, reject


def require_contribution(candidate: object) -> Verdict | None:
    """Return a rejection when ``candidate`` is not an identity-free proposal."""

    if not isinstance(candidate, ResearchContribution):
        return reject(
            "CANDIDATE_NOT_IDENTITY_FREE",
            f"expected ResearchContribution, got {type(candidate).__name__}",
        )
    return None


def _schema_rejection(error: AgendaSchemaError) -> Verdict:
    code = error.code if error.code in VERDICT_CODES else "PAYLOAD_MALFORMED"
    return reject(code, error.detail)


def parse_candidate(
    candidate: object,
    *,
    schema: str,
    parser: Callable[[object], Any],
) -> tuple[Any | None, Verdict | None]:
    """Validate candidate shape, kind binding and payload in one step.

    Returns ``(parsed_payload, None)`` on success or ``(None, verdict)`` on the
    first rejection.
    """

    rejection = require_contribution(candidate)
    if rejection is not None:
        return None, rejection
    assert isinstance(candidate, ResearchContribution)
    allowed = TREATMENT_KIND_BINDING[schema]
    if candidate.kind not in allowed:
        return None, reject(
            "RECORD_KIND_NOT_ALLOWED",
            f"{schema} accepts {sorted(allowed)!r}, got {candidate.kind!r}",
        )
    try:
        parsed = parser(candidate.payload)
    except AgendaSchemaError as error:
        return None, _schema_rejection(error)
    except ValueError as error:  # defensive: any other strict-parse failure
        return None, reject("PAYLOAD_MALFORMED", str(error)[:256])
    return parsed, None
