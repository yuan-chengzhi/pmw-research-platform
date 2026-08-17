"""Stable verdict vocabulary for agenda-treatment validators.

Validators never raise on ordinary rejection.  They return a :class:`Verdict`
whose ``code`` is drawn from a closed, stable set, so an experiment log can be
compared across runs without parsing prose.
"""

from __future__ import annotations

from dataclasses import dataclass


ACCEPTED = "ACCEPTED"

MAXIMUM_DETAIL_BYTES = 2_000

VERDICT_CODES = frozenset({
    ACCEPTED,
    # Candidate-shape rejections.
    "CANDIDATE_NOT_IDENTITY_FREE",
    "PAYLOAD_SCHEMA_MISMATCH",
    "PAYLOAD_MALFORMED",
    "RECORD_KIND_NOT_ALLOWED",
    "IDENTITY_FIELD_SELF_ASSERTED",
    # The host did not supply the identity it would inject at ``bind``, so no
    # authority question about this candidate can be answered.
    "AUTHOR_IDENTITY_REQUIRED",
    # Worklist (D-arm) rejections.
    "TASK_UNKNOWN",
    "TASK_ALREADY_COMPLETED",
    "TASK_DEPENDENCY_UNKNOWN",
    "TASK_DEPENDENCIES_UNREADY",
    "TASK_CLAIM_CONFLICT",
    "CLAIM_UNKNOWN",
    "CLAIM_TASK_MISMATCH",
    "CLAIM_NOT_HELD_BY_AUTHOR",
    "CLAIM_ALREADY_CLOSED",
    "LEASE_EXPIRED",
    "LEASE_LIVENESS_UNDECIDABLE",
    "PROPOSAL_UNKNOWN",
    "COMPLETION_CONTRACT_MISMATCH",
    "COMPLETION_EVIDENCE_MISSING",
    "NOT_AN_ADMITTING_SLOT",
    # Central (C-arm) rejections.
    "NOT_A_COORDINATOR_SLOT",
    "DIRECTIVE_UNKNOWN",
    "DIRECTIVE_NOT_LIVE",
    "DIRECTIVE_CITATION_MISSING",
    "SUPERSEDED_DIRECTIVE_UNKNOWN",
    # Adaptive-trigger rejections.
    "DECOMPOSITION_TARGET_UNKNOWN",
    "DECOMPOSITION_SUBLEMMA_UNKNOWN",
    "DECOMPOSITION_STATEMENT_MISMATCH",
})


@dataclass(frozen=True, slots=True)
class Verdict:
    """One immutable decision about a single candidate at a single snapshot."""

    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if type(self.code) is not str or self.code not in VERDICT_CODES:
            raise ValueError(f"unknown verdict code: {self.code!r}")
        if type(self.detail) is not str:
            raise TypeError("verdict detail must be str")
        encoded = self.detail.encode("utf-8", errors="replace")
        if len(encoded) > MAXIMUM_DETAIL_BYTES:
            object.__setattr__(
                self,
                "detail",
                encoded[:MAXIMUM_DETAIL_BYTES].decode("utf-8", errors="ignore"),
            )

    @property
    def accepted(self) -> bool:
        """Return whether the candidate may be admitted under this treatment.

        Deliberately explicit: :class:`Verdict` defines no ``__bool__`` so a
        caller cannot accidentally treat a rejection as a truthy object.
        """

        return self.code == ACCEPTED

    def to_value(self) -> dict[str, object]:
        return {"accepted": self.accepted, "code": self.code, "detail": self.detail}


def accept(detail: str = "") -> Verdict:
    return Verdict(ACCEPTED, detail)


def reject(code: str, detail: str = "") -> Verdict:
    if code == ACCEPTED:
        raise ValueError("reject() requires a rejection code")
    return Verdict(code, detail)
