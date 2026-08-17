"""Typed usage evidence for research sessions.

A receipt must never leave the reader guessing whether a token count is a
measurement, a claim, or nothing at all.  This module therefore carries three
mutually exclusive states:

``MEASURED``
    A trusted adapter read the numbers off a surface it can point at.  The
    ``provenance`` token names that surface (for example ``PI_RPC_REPORTED``).
``ASSERTED``
    Nobody measured anything.  A named party asserts a count because of the
    profile it runs, and the assertion is labeled as such.  A hardcoded zero
    belongs here and nowhere else.
``UNMEASURED``
    No usage surface answered.  This is a first-class honest outcome, not a
    zero, and it never silently becomes one.

Per-request records stay exactly as the surface reported them; the host never
back-fills a missing field with a guess.  Aggregate readings each carry the
``basis`` that produced them, so a host-summed total and a runtime-reported
session total can disagree in the open instead of being merged into one
unattributable number.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Mapping, Sequence


USAGE_EVIDENCE_SCHEMA = "PMW_RUNTIME_USAGE_EVIDENCE_1"

# Provenance tokens used by the platform's own backends.  ``provenance`` is an
# open uppercase vocabulary — like ``terminal_reason`` — so a backend can name
# its own surface without this backend-neutral module learning about it.
PROVENANCE_PI_RPC_REPORTED = "PI_RPC_REPORTED"
PROVENANCE_PI_RPC_SURFACE_SILENT = "PI_RPC_SURFACE_SILENT"
PROVENANCE_BACKEND_SELF_REPORT = "BACKEND_SELF_REPORT"
PROVENANCE_COMMAND_BACKEND_MODEL_FREE_PROFILE = (
    "COMMAND_BACKEND_MODEL_FREE_PROFILE"
)
PROVENANCE_BACKEND_DECLARED_NO_USAGE_EVIDENCE = (
    "BACKEND_DECLARED_NO_USAGE_EVIDENCE"
)
PROVENANCE_NO_BACKEND_OUTCOME = "NO_BACKEND_OUTCOME"

BASIS_HOST_SUMMED_OBSERVED_RECORDS = "HOST_SUMMED_OBSERVED_RECORDS"
BASIS_RUNTIME_REPORTED_SESSION_TOTALS = "RUNTIME_REPORTED_SESSION_TOTALS"

MAXIMUM_USAGE_REQUEST_RECORDS = 4_096
MAXIMUM_USAGE_TOTALS = 8
MAXIMUM_USAGE_ASSERTION_ENTRIES = 16
MAXIMUM_TOKEN_COUNT = (1 << 53) - 1
MAXIMUM_USAGE_DETAIL_BYTES = 2_048

_TOKEN_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_ASSERTION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FREE_LABEL = re.compile(r"^[^\x00\r\n]{1,512}$")

_REQUEST_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)
_REQUEST_FIELDS = frozenset(
    {"ordinal", "source_event", "role", "provider", "model", "stop_reason"}
    | set(_REQUEST_TOKEN_FIELDS)
)
_TOTALS_FIELDS = frozenset(
    {"basis", "request_count"} | set(_REQUEST_TOKEN_FIELDS)
)
_EVIDENCE_FIELDS = frozenset({
    "schema",
    "state",
    "provenance",
    "detail",
    "requests",
    "requests_truncated",
    "totals",
    "provider_reported_context_tokens",
    "provider_reported_context_window_tokens",
    "assertion",
})


class UsageEvidenceError(ValueError):
    """A usage evidence value is malformed or mislabels its own state."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


class UsageState(str, Enum):
    """How a usage block came to hold the numbers it holds."""

    MEASURED = "MEASURED"
    ASSERTED = "ASSERTED"
    UNMEASURED = "UNMEASURED"


def _uppercase_token(value: object, *, label: str) -> str:
    if type(value) is not str or _TOKEN_NAME.fullmatch(value) is None:
        raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", label)
    return value


def _detail(value: object) -> str:
    if (
        type(value) is not str
        or len(value.encode("utf-8", errors="strict"))
        > MAXIMUM_USAGE_DETAIL_BYTES
    ):
        raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "detail")
    return value


def _optional_label(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _FREE_LABEL.fullmatch(value) is None:
        raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", label)
    return value


def _optional_count(value: object, *, label: str) -> int | None:
    """Return a bounded non-negative count, or ``None`` for "not reported"."""

    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAXIMUM_TOKEN_COUNT:
        raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", label)
    return value


def observed_count(value: object) -> int | None:
    """Read one token count from an untrusted runtime frame.

    Anything that is not a bounded non-negative integer is *not reported*
    rather than zero: a surface that answers with junk has still not measured
    anything, and the difference must survive into the receipt.
    """

    # ``type(...) is int`` also rejects ``bool``, which a runtime must never
    # be allowed to pass off as a token count.
    if type(value) is not int or not 0 <= value <= MAXIMUM_TOKEN_COUNT:
        return None
    return value


@dataclass(frozen=True, slots=True)
class UsageRequestRecord:
    """One provider request exactly as a runtime surface reported it."""

    ordinal: int
    source_event: str
    role: str
    provider: str | None = None
    model: str | None = None
    stop_reason: str | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.ordinal) is not int
            or not 1 <= self.ordinal <= MAXIMUM_USAGE_REQUEST_RECORDS
        ):
            raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "ordinal")
        for label in ("source_event", "role"):
            selected = getattr(self, label)
            if type(selected) is not str or _FREE_LABEL.fullmatch(selected) is None:
                raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", label)
        for label in ("provider", "model", "stop_reason"):
            _optional_label(getattr(self, label), label=label)
        for label in _REQUEST_TOKEN_FIELDS:
            _optional_count(getattr(self, label), label=label)

    @property
    def reported_any_count(self) -> bool:
        return any(
            getattr(self, label) is not None for label in _REQUEST_TOKEN_FIELDS
        )

    def to_value(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "source_event": self.source_event,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "stop_reason": self.stop_reason,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """One aggregate reading together with the basis that produced it."""

    basis: str
    request_count: int | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        _uppercase_token(self.basis, label="basis")
        _optional_count(self.request_count, label="request_count")
        for label in _REQUEST_TOKEN_FIELDS:
            _optional_count(getattr(self, label), label=label)

    @property
    def reported_any_count(self) -> bool:
        return any(
            getattr(self, label) is not None for label in _REQUEST_TOKEN_FIELDS
        )

    def to_value(self) -> dict[str, object]:
        return {
            "basis": self.basis,
            "request_count": self.request_count,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class UsageEvidence:
    """A receipt-ready usage block that states its own epistemic status."""

    state: UsageState
    provenance: str
    detail: str = ""
    requests: tuple[UsageRequestRecord, ...] = ()
    requests_truncated: bool = False
    totals: tuple[UsageTotals, ...] = ()
    provider_reported_context_tokens: int | None = None
    provider_reported_context_window_tokens: int | None = None
    assertion: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, UsageState):
            raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "state")
        _uppercase_token(self.provenance, label="provenance")
        _detail(self.detail)
        if type(self.requests_truncated) is not bool:
            raise UsageEvidenceError(
                "MALFORMED_USAGE_EVIDENCE", "requests_truncated"
            )
        if (
            not isinstance(self.requests, tuple)
            or len(self.requests) > MAXIMUM_USAGE_REQUEST_RECORDS
            or any(
                not isinstance(item, UsageRequestRecord)
                for item in self.requests
            )
        ):
            raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "requests")
        if [item.ordinal for item in self.requests] != list(
            range(1, len(self.requests) + 1)
        ):
            raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "ordinal run")
        if (
            not isinstance(self.totals, tuple)
            or len(self.totals) > MAXIMUM_USAGE_TOTALS
            or any(not isinstance(item, UsageTotals) for item in self.totals)
        ):
            raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "totals")
        if len({item.basis for item in self.totals}) != len(self.totals):
            raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "totals basis")
        for label in (
            "provider_reported_context_tokens",
            "provider_reported_context_window_tokens",
        ):
            _optional_count(getattr(self, label), label=label)
        if not isinstance(self.assertion, tuple) or len(
            self.assertion
        ) > MAXIMUM_USAGE_ASSERTION_ENTRIES:
            raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "assertion")
        names: list[str] = []
        for entry in self.assertion:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or type(entry[0]) is not str
                or _ASSERTION_NAME.fullmatch(entry[0]) is None
            ):
                raise UsageEvidenceError(
                    "MALFORMED_USAGE_EVIDENCE", "assertion entry"
                )
            _optional_count(entry[1], label="assertion value")
            if entry[1] is None:
                raise UsageEvidenceError(
                    "MALFORMED_USAGE_EVIDENCE", "assertion value"
                )
            names.append(entry[0])
        if sorted(names) != names or len(set(names)) != len(names):
            raise UsageEvidenceError(
                "MALFORMED_USAGE_EVIDENCE", "assertion order"
            )
        self._check_state_invariants()

    def _check_state_invariants(self) -> None:
        """Reject any block whose contents contradict its declared state."""

        measured_content = bool(self.requests) or any(
            item.reported_any_count for item in self.totals
        )
        if self.state is UsageState.MEASURED:
            if not measured_content:
                raise UsageEvidenceError(
                    "MALFORMED_USAGE_EVIDENCE", "measured without a reading"
                )
            if self.assertion:
                raise UsageEvidenceError(
                    "MALFORMED_USAGE_EVIDENCE", "measured with an assertion"
                )
            return
        # Neither an assertion nor an unmeasured marker may smuggle a number
        # in; that is exactly the confusion this type exists to prevent.
        if (
            measured_content
            or self.totals
            or self.provider_reported_context_tokens is not None
            or self.provider_reported_context_window_tokens is not None
        ):
            raise UsageEvidenceError(
                "MALFORMED_USAGE_EVIDENCE", "unmeasured with a reading"
            )
        if self.state is UsageState.ASSERTED and not self.assertion:
            raise UsageEvidenceError(
                "MALFORMED_USAGE_EVIDENCE", "assertion is empty"
            )
        if self.state is UsageState.UNMEASURED and self.assertion:
            raise UsageEvidenceError(
                "MALFORMED_USAGE_EVIDENCE", "unmeasured with an assertion"
            )

    @classmethod
    def unmeasured(cls, *, provenance: str, detail: str = "") -> "UsageEvidence":
        """No usage surface answered; say so instead of reporting zero."""

        return cls(
            state=UsageState.UNMEASURED,
            provenance=provenance,
            detail=detail,
        )

    @classmethod
    def asserted(
        cls,
        *,
        provenance: str,
        assertion: Mapping[str, int],
        detail: str = "",
    ) -> "UsageEvidence":
        """A labeled profile claim.  It is not, and never becomes, a reading."""

        if type(assertion) is not dict:
            raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "assertion")
        return cls(
            state=UsageState.ASSERTED,
            provenance=provenance,
            detail=detail,
            assertion=tuple(sorted(assertion.items())),
        )

    @classmethod
    def measured(
        cls,
        *,
        provenance: str,
        requests: Sequence[UsageRequestRecord] = (),
        requests_truncated: bool = False,
        totals: Sequence[UsageTotals] = (),
        provider_reported_context_tokens: int | None = None,
        provider_reported_context_window_tokens: int | None = None,
        detail: str = "",
    ) -> "UsageEvidence":
        return cls(
            state=UsageState.MEASURED,
            provenance=provenance,
            detail=detail,
            requests=tuple(requests),
            requests_truncated=requests_truncated,
            totals=tuple(totals),
            provider_reported_context_tokens=provider_reported_context_tokens,
            provider_reported_context_window_tokens=(
                provider_reported_context_window_tokens
            ),
        )

    @property
    def measured_state(self) -> bool:
        return self.state is UsageState.MEASURED

    def to_value(self) -> dict[str, object]:
        return {
            "schema": USAGE_EVIDENCE_SCHEMA,
            "state": self.state.value,
            "provenance": self.provenance,
            "detail": self.detail,
            "requests": [item.to_value() for item in self.requests],
            "requests_truncated": self.requests_truncated,
            "totals": [item.to_value() for item in self.totals],
            "provider_reported_context_tokens": (
                self.provider_reported_context_tokens
            ),
            "provider_reported_context_window_tokens": (
                self.provider_reported_context_window_tokens
            ),
            "assertion": (
                None
                if not self.assertion
                else {name: value for name, value in self.assertion}
            ),
        }

    @classmethod
    def from_value(cls, value: object) -> "UsageEvidence":
        """Rebuild a usage block from a durable receipt document."""

        if type(value) is not dict or set(value) != _EVIDENCE_FIELDS:
            raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "fields")
        if value.get("schema") != USAGE_EVIDENCE_SCHEMA:
            raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "schema")
        raw_state = value.get("state")
        try:
            state = UsageState(raw_state)
        except ValueError as error:
            raise UsageEvidenceError(
                "MALFORMED_USAGE_EVIDENCE", "state"
            ) from error
        raw_requests = value.get("requests")
        raw_totals = value.get("totals")
        if type(raw_requests) is not list or type(raw_totals) is not list:
            raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "sequences")
        raw_assertion = value.get("assertion")
        if raw_assertion is not None and type(raw_assertion) is not dict:
            raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "assertion")
        return cls(
            state=state,
            provenance=value.get("provenance"),  # type: ignore[arg-type]
            detail=value.get("detail"),  # type: ignore[arg-type]
            requests=tuple(
                _request_from_value(item) for item in raw_requests
            ),
            requests_truncated=value.get(  # type: ignore[arg-type]
                "requests_truncated"
            ),
            totals=tuple(_totals_from_value(item) for item in raw_totals),
            provider_reported_context_tokens=value.get(  # type: ignore[arg-type]
                "provider_reported_context_tokens"
            ),
            provider_reported_context_window_tokens=value.get(  # type: ignore[arg-type]
                "provider_reported_context_window_tokens"
            ),
            assertion=(
                ()
                if raw_assertion is None
                else tuple(sorted(raw_assertion.items()))
            ),
        )


def _request_from_value(value: object) -> UsageRequestRecord:
    if type(value) is not dict or set(value) != _REQUEST_FIELDS:
        raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "request fields")
    return UsageRequestRecord(**value)  # type: ignore[arg-type]


def _totals_from_value(value: object) -> UsageTotals:
    if type(value) is not dict or set(value) != _TOTALS_FIELDS:
        raise UsageEvidenceError("MALFORMED_USAGE_EVIDENCE", "totals fields")
    return UsageTotals(**value)  # type: ignore[arg-type]


def summed_totals(
    records: Sequence[UsageRequestRecord],
    *,
    basis: str = BASIS_HOST_SUMMED_OBSERVED_RECORDS,
) -> UsageTotals:
    """Sum observed records, leaving wholly unreported fields unreported.

    A field is summed only over the records that actually reported it, and a
    field no record reported stays ``None``.  Summing absence into zero is the
    precise error this whole module exists to make impossible.
    """

    sums: dict[str, int | None] = {label: None for label in _REQUEST_TOKEN_FIELDS}
    for record in records:
        for label in _REQUEST_TOKEN_FIELDS:
            reported = getattr(record, label)
            if reported is None:
                continue
            running = sums[label]
            sums[label] = reported if running is None else running + reported
    return UsageTotals(basis=basis, request_count=len(records), **sums)


def usage_evidence_value_is_valid(value: object) -> bool:
    """Return whether a durable document holds a well-formed usage block."""

    try:
        UsageEvidence.from_value(value)
    except (UsageEvidenceError, TypeError):
        return False
    return True


__all__ = [
    "BASIS_HOST_SUMMED_OBSERVED_RECORDS",
    "BASIS_RUNTIME_REPORTED_SESSION_TOTALS",
    "MAXIMUM_TOKEN_COUNT",
    "MAXIMUM_USAGE_REQUEST_RECORDS",
    "PROVENANCE_BACKEND_DECLARED_NO_USAGE_EVIDENCE",
    "PROVENANCE_BACKEND_SELF_REPORT",
    "PROVENANCE_COMMAND_BACKEND_MODEL_FREE_PROFILE",
    "PROVENANCE_NO_BACKEND_OUTCOME",
    "PROVENANCE_PI_RPC_REPORTED",
    "PROVENANCE_PI_RPC_SURFACE_SILENT",
    "USAGE_EVIDENCE_SCHEMA",
    "UsageEvidence",
    "UsageEvidenceError",
    "UsageRequestRecord",
    "UsageState",
    "UsageTotals",
    "observed_count",
    "summed_totals",
    "usage_evidence_value_is_valid",
]
