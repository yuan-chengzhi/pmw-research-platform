"""Typed agenda-treatment payloads that ride the six existing record kinds.

The platform's contribution vocabulary is closed: ``NOTE``, ``NEED``,
``ATTEMPT``, ``RESULT``, ``OBJECTION`` and ``CHECKPOINT``.  This module adds no
seventh kind.  Every treatment record is an ordinary
:class:`~pmw_platform.world.records.ResearchContribution` whose ``payload``
carries a discriminated schema object, plus a declared binding that fixes which
of the six kinds may carry it.

Two invariants are load-bearing:

* **Identity-free.**  No treatment payload has a field naming its author, its
  cohort, its world or its base snapshot.  The claimant of a lease is the
  ``session_id`` the trusted host injects at ``ResearchContribution.bind``.
  :func:`reject_self_asserted_identity` enforces this at any nesting depth.
* **Time-free.**  No treatment payload carries a wall-clock or a lease start
  time.  A lease declares only a *duration*; when it started is host runtime
  evidence supplied to :class:`~.snapshot.AgendaSnapshot`, never a value the
  research process can author.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Mapping, NoReturn, Sequence

from ...world.records import RESEARCH_KINDS, ResearchContribution


TASK_PROPOSAL_SCHEMA = "PMW_AGENDA_TASK_PROPOSAL_1"
TASK_ADMISSION_SCHEMA = "PMW_AGENDA_TASK_ADMISSION_1"
TASK_CLAIM_SCHEMA = "PMW_AGENDA_TASK_CLAIM_1"
TASK_RELEASE_SCHEMA = "PMW_AGENDA_TASK_RELEASE_1"
TASK_OUTCOME_SCHEMA = "PMW_AGENDA_TASK_OUTCOME_1"
DIRECTIVE_SCHEMA = "PMW_AGENDA_DIRECTIVE_1"
DECOMPOSITION_SCHEMA = "PMW_AGENDA_DECOMPOSITION_1"

AGENDA_TREATMENT_SCHEMAS = frozenset({
    TASK_PROPOSAL_SCHEMA,
    TASK_ADMISSION_SCHEMA,
    TASK_CLAIM_SCHEMA,
    TASK_RELEASE_SCHEMA,
    TASK_OUTCOME_SCHEMA,
    DIRECTIVE_SCHEMA,
    DECOMPOSITION_SCHEMA,
})

# Which of the six existing kinds may carry each treatment payload.  A record
# whose kind is outside its schema's binding is rejected; the binding is the
# only place a new treatment touches the kind vocabulary.
TREATMENT_KIND_BINDING: Mapping[str, frozenset[str]] = {
    # A proposal is an identified piece of work that still needs doing.
    TASK_PROPOSAL_SCHEMA: frozenset({"NEED"}),
    # An admission is a reviewed agenda state: this task is on the worklist.
    TASK_ADMISSION_SCHEMA: frozenset({"CHECKPOINT"}),
    # Lease bookkeeping is a coordination announcement, not a mathematical
    # claim, so it rides NOTE rather than RESULT.
    TASK_CLAIM_SCHEMA: frozenset({"NOTE"}),
    TASK_RELEASE_SCHEMA: frozenset({"NOTE"}),
    # An outcome's kind must agree with its disposition; see
    # OUTCOME_DISPOSITION_KIND below.
    TASK_OUTCOME_SCHEMA: frozenset({"RESULT", "ATTEMPT"}),
    DIRECTIVE_SCHEMA: frozenset({"CHECKPOINT"}),
    # A decomposition asserts that sublemmas jointly suffice for a target.
    # That is a mathematical claim, so it rides RESULT.
    DECOMPOSITION_SCHEMA: frozenset({"RESULT"}),
}

# Enforced at import: the plugin rides the closed six-kind vocabulary and can
# never introduce a seventh kind by editing a binding.
for _schema, _kinds in TREATMENT_KIND_BINDING.items():
    if not _kinds or not _kinds <= RESEARCH_KINDS:
        raise AssertionError(
            f"{_schema} binds kinds outside the platform vocabulary: "
            f"{sorted(_kinds - RESEARCH_KINDS)!r}"
        )
del _schema, _kinds

OUTCOME_DISPOSITIONS = ("ABANDONED", "BLOCKED", "COMPLETED")
OUTCOME_DISPOSITION_KIND: Mapping[str, str] = {
    "COMPLETED": "RESULT",
    "ABANDONED": "ATTEMPT",
    "BLOCKED": "ATTEMPT",
}

COMPLETION_CONTRACT_KINDS = (
    # Settled by the pinned AMF verifier against a briefing-bound target.  The
    # plugin checks that evidence is *declared and shaped*; it never re-runs a
    # verifier and never asserts the mathematics is correct.
    "AMF_VERIFIER_PASS",
    # Settled by an admitted CHECKPOINT that reviewed the outcome.
    "PEER_CHECKPOINT",
    # Settled by the claimant's own declaration.  The weakest contract; kept
    # explicit so an experiment can measure how often it is chosen.
    "DECLARED_STATEMENT",
)

# Contracts whose completion evidence must additionally reference at least one
# artifact on the outcome record itself.  The artifact CAS closure is already
# checked by the host at publish time, so the treatment reuses it instead of
# duplicating digests inside the payload.
ARTIFACT_BACKED_CONTRACT_KINDS = frozenset({"AMF_VERIFIER_PASS"})

# Kinds that commit or advance the research line.  Under the C arm these must
# cite a live directive.  NOTE, NEED and OBJECTION are deliberately excluded:
# a treatment that could silence an OBJECTION for lack of a directive would
# corrupt the evidence it is meant to measure.
PRIMARY_ACTION_KINDS = frozenset({"ATTEMPT", "RESULT", "CHECKPOINT"})

# Payload field through which a primary action record cites its directives.
# Citation deliberately does not overload ``parent_refs``, which stays
# available for genuine mathematical lineage.
CITED_DIRECTIVE_REFS_FIELD = "cited_directive_refs"

# Payload keys that only the trusted host may ever determine.  A research
# process that writes one of these into a payload is asserting an identity it
# does not own, at any nesting depth.
HOST_INJECTED_IDENTITY_KEYS = frozenset({
    "agent_id",
    "base_snapshot_ref",
    "claimant",
    "claimant_session_id",
    "cohort_id",
    "holder_session_id",
    "principal_ref",
    "session_id",
    "world_id",
    "world_ref",
})

MAXIMUM_STATEMENT_BYTES = 8_000
MAXIMUM_DETAIL_BYTES = 8_000
MAXIMUM_INSTRUCTION_BYTES = 8_000
MAXIMUM_DEPENDENCY_REFS = 32
MAXIMUM_SUPERSEDES_REFS = 16
MAXIMUM_CITED_DIRECTIVE_REFS = 16
MAXIMUM_SUBLEMMAS = 64
MAXIMUM_LEASE_TICKS = 1 << 31

_ADMISSION_REF = re.compile(r"^admission/sha256/[0-9a-f]{64}$")
_DIRECTIVE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PROBLEM_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class AgendaSchemaError(ValueError):
    """Stable validation error for one proposed agenda-treatment payload."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _fail(code: str, detail: str = "") -> NoReturn:
    raise AgendaSchemaError(code, detail)


def _malformed(detail: str) -> NoReturn:
    _fail("PAYLOAD_MALFORMED", detail)


def _text(value: object, *, label: str, maximum_bytes: int) -> str:
    if type(value) is not str or not value:
        _malformed(label)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise AgendaSchemaError("PAYLOAD_MALFORMED", label) from error
    if len(encoded) > maximum_bytes:
        _malformed(f"{label}: too long")
    return value


def _admission_ref(value: object, *, label: str) -> str:
    if type(value) is not str or _ADMISSION_REF.fullmatch(value) is None:
        _malformed(label)
    return value


def _identifier(value: object, *, label: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _malformed(label)
    return value


def _positive_integer(value: object, *, label: str, maximum: int) -> int:
    if type(value) is not int or isinstance(value, bool) or not 1 <= value <= maximum:
        _malformed(label)
    return value


def _member(value: object, *, label: str, allowed: Sequence[str]) -> str:
    if type(value) is not str or value not in allowed:
        _malformed(label)
    return value


def _exact_fields(value: object, *, label: str, expected: frozenset[str]) -> Mapping[str, object]:
    if type(value) is not dict:
        _malformed(label)
    if set(value) != expected:
        _malformed(f"{label}: fields {sorted(set(value))!r}")
    return value


def _ref_tuple(
    values: object,
    *,
    label: str,
    maximum: int,
) -> tuple[str, ...]:
    if type(values) is not list:
        _malformed(label)
    if len(values) > maximum:
        _malformed(f"{label}: too many")
    selected = tuple(_admission_ref(item, label=label) for item in values)
    if len(selected) != len(set(selected)):
        _malformed(f"{label}: duplicate")
    if list(selected) != sorted(selected):
        _malformed(f"{label}: unsorted")
    return selected


def reject_self_asserted_identity(payload: object) -> None:
    """Raise if a payload names an identity only the host may determine.

    The check walks the whole payload, not just its top level: nesting a
    ``session_id`` inside a completion contract would be exactly as much of a
    boundary violation as declaring it beside ``task_ref``.
    """

    stack: list[object] = [payload]
    while stack:
        item = stack.pop()
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is str and key in HOST_INJECTED_IDENTITY_KEYS:
                    _fail("IDENTITY_FIELD_SELF_ASSERTED", key)
                stack.append(child)
        elif type(item) is list:
            stack.extend(item)


@dataclass(frozen=True, slots=True)
class CompletionContract:
    """What would settle a task, declared before the work starts."""

    contract_kind: str
    target_id: str | None
    detail: str

    def to_value(self) -> dict[str, object]:
        return {
            "contract_kind": self.contract_kind,
            "target_id": self.target_id,
            "detail": self.detail,
        }

    @classmethod
    def parse(cls, value: object, *, label: str = "completion_contract") -> "CompletionContract":
        selected = _exact_fields(
            value,
            label=label,
            expected=frozenset({"contract_kind", "target_id", "detail"}),
        )
        target = selected["target_id"]
        if target is not None:
            target = _identifier(target, label=f"{label}.target_id", pattern=_PROBLEM_ID)
        return cls(
            contract_kind=_member(
                selected["contract_kind"],
                label=f"{label}.contract_kind",
                allowed=COMPLETION_CONTRACT_KINDS,
            ),
            target_id=target,
            detail=_text(
                selected["detail"],
                label=f"{label}.detail",
                maximum_bytes=MAXIMUM_DETAIL_BYTES,
            ),
        )


@dataclass(frozen=True, slots=True)
class CompletionEvidence:
    """What the claimant offers against the admitted completion contract."""

    contract_kind: str
    detail: str

    def to_value(self) -> dict[str, object]:
        return {"contract_kind": self.contract_kind, "detail": self.detail}

    @classmethod
    def parse(cls, value: object, *, label: str = "completion_evidence") -> "CompletionEvidence":
        selected = _exact_fields(
            value, label=label, expected=frozenset({"contract_kind", "detail"})
        )
        return cls(
            contract_kind=_member(
                selected["contract_kind"],
                label=f"{label}.contract_kind",
                allowed=COMPLETION_CONTRACT_KINDS,
            ),
            detail=_text(
                selected["detail"],
                label=f"{label}.detail",
                maximum_bytes=MAXIMUM_DETAIL_BYTES,
            ),
        )


@dataclass(frozen=True, slots=True)
class TaskProposalPayload:
    """Any peer may propose work; a proposal grants no lease and no authority."""

    statement: str
    dependency_task_refs: tuple[str, ...]
    completion_contract: CompletionContract

    def to_value(self) -> dict[str, object]:
        return {
            "schema": TASK_PROPOSAL_SCHEMA,
            "statement": self.statement,
            "dependency_task_refs": list(self.dependency_task_refs),
            "completion_contract": self.completion_contract.to_value(),
        }

    @classmethod
    def parse(cls, value: object) -> "TaskProposalPayload":
        selected = _typed_fields(
            value,
            schema=TASK_PROPOSAL_SCHEMA,
            expected=frozenset({
                "schema",
                "statement",
                "dependency_task_refs",
                "completion_contract",
            }),
        )
        return cls(
            statement=_text(
                selected["statement"],
                label="statement",
                maximum_bytes=MAXIMUM_STATEMENT_BYTES,
            ),
            dependency_task_refs=_ref_tuple(
                selected["dependency_task_refs"],
                label="dependency_task_refs",
                maximum=MAXIMUM_DEPENDENCY_REFS,
            ),
            completion_contract=CompletionContract.parse(
                selected["completion_contract"]
            ),
        )


@dataclass(frozen=True, slots=True)
class TaskAdmissionPayload:
    """The authoritative worklist entry; its admission ref is the task identity.

    Dependencies name admission refs that must already exist in the snapshot,
    so a dependency graph built from admitted tasks is acyclic by construction:
    a record cannot reference a ref that did not exist when it was written.
    """

    statement: str
    dependency_task_refs: tuple[str, ...]
    completion_contract: CompletionContract
    proposal_ref: str | None

    def to_value(self) -> dict[str, object]:
        return {
            "schema": TASK_ADMISSION_SCHEMA,
            "statement": self.statement,
            "dependency_task_refs": list(self.dependency_task_refs),
            "completion_contract": self.completion_contract.to_value(),
            "proposal_ref": self.proposal_ref,
        }

    @classmethod
    def parse(cls, value: object) -> "TaskAdmissionPayload":
        selected = _typed_fields(
            value,
            schema=TASK_ADMISSION_SCHEMA,
            expected=frozenset({
                "schema",
                "statement",
                "dependency_task_refs",
                "completion_contract",
                "proposal_ref",
            }),
        )
        proposal = selected["proposal_ref"]
        if proposal is not None:
            proposal = _admission_ref(proposal, label="proposal_ref")
        return cls(
            statement=_text(
                selected["statement"],
                label="statement",
                maximum_bytes=MAXIMUM_STATEMENT_BYTES,
            ),
            dependency_task_refs=_ref_tuple(
                selected["dependency_task_refs"],
                label="dependency_task_refs",
                maximum=MAXIMUM_DEPENDENCY_REFS,
            ),
            completion_contract=CompletionContract.parse(
                selected["completion_contract"]
            ),
            proposal_ref=proposal,
        )


@dataclass(frozen=True, slots=True)
class TaskClaimPayload:
    """An exclusive lease request declaring only its duration.

    ``lease_ticks`` is a duration in the host's agenda-clock unit.  The tick at
    which the lease started is host runtime evidence carried by the snapshot,
    never a field the research process can author, so a claimant cannot extend
    its own lease by backdating or forward-dating a start time.
    """

    task_ref: str
    lease_ticks: int

    def to_value(self) -> dict[str, object]:
        return {
            "schema": TASK_CLAIM_SCHEMA,
            "task_ref": self.task_ref,
            "lease_ticks": self.lease_ticks,
        }

    @classmethod
    def parse(cls, value: object) -> "TaskClaimPayload":
        selected = _typed_fields(
            value,
            schema=TASK_CLAIM_SCHEMA,
            expected=frozenset({"schema", "task_ref", "lease_ticks"}),
        )
        return cls(
            task_ref=_admission_ref(selected["task_ref"], label="task_ref"),
            lease_ticks=_positive_integer(
                selected["lease_ticks"],
                label="lease_ticks",
                maximum=MAXIMUM_LEASE_TICKS,
            ),
        )


@dataclass(frozen=True, slots=True)
class TaskReleasePayload:
    """Close a lease without asserting an outcome; the reason belongs in ``body``."""

    task_ref: str
    claim_ref: str

    def to_value(self) -> dict[str, object]:
        return {
            "schema": TASK_RELEASE_SCHEMA,
            "task_ref": self.task_ref,
            "claim_ref": self.claim_ref,
        }

    @classmethod
    def parse(cls, value: object) -> "TaskReleasePayload":
        selected = _typed_fields(
            value,
            schema=TASK_RELEASE_SCHEMA,
            expected=frozenset({"schema", "task_ref", "claim_ref"}),
        )
        return cls(
            task_ref=_admission_ref(selected["task_ref"], label="task_ref"),
            claim_ref=_admission_ref(selected["claim_ref"], label="claim_ref"),
        )


@dataclass(frozen=True, slots=True)
class TaskOutcomePayload:
    """Close a lease with a disposition against the admitted contract."""

    task_ref: str
    claim_ref: str
    disposition: str
    completion_evidence: CompletionEvidence | None

    def to_value(self) -> dict[str, object]:
        evidence = self.completion_evidence
        return {
            "schema": TASK_OUTCOME_SCHEMA,
            "task_ref": self.task_ref,
            "claim_ref": self.claim_ref,
            "disposition": self.disposition,
            "completion_evidence": None if evidence is None else evidence.to_value(),
        }

    @classmethod
    def parse(cls, value: object) -> "TaskOutcomePayload":
        selected = _typed_fields(
            value,
            schema=TASK_OUTCOME_SCHEMA,
            expected=frozenset({
                "schema",
                "task_ref",
                "claim_ref",
                "disposition",
                "completion_evidence",
            }),
        )
        disposition = _member(
            selected["disposition"],
            label="disposition",
            allowed=OUTCOME_DISPOSITIONS,
        )
        raw_evidence = selected["completion_evidence"]
        if disposition == "COMPLETED":
            if raw_evidence is None:
                _fail("COMPLETION_EVIDENCE_MISSING", "COMPLETED outcome")
            evidence: CompletionEvidence | None = CompletionEvidence.parse(raw_evidence)
        else:
            if raw_evidence is not None:
                _malformed("completion_evidence: only a COMPLETED outcome carries it")
            evidence = None
        return cls(
            task_ref=_admission_ref(selected["task_ref"], label="task_ref"),
            claim_ref=_admission_ref(selected["claim_ref"], label="claim_ref"),
            disposition=disposition,
            completion_evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class DirectivePayload:
    """A coordinator instruction.

    Liveness is snapshot-local and needs no revocation flag: a directive stops
    being live exactly when a later valid coordinator directive supersedes it.
    Revoking without replacement is therefore expressed as a superseding
    directive that says so.
    """

    directive_id: str
    instruction: str
    supersedes_refs: tuple[str, ...]

    def to_value(self) -> dict[str, object]:
        return {
            "schema": DIRECTIVE_SCHEMA,
            "directive_id": self.directive_id,
            "instruction": self.instruction,
            "supersedes_refs": list(self.supersedes_refs),
        }

    @classmethod
    def parse(cls, value: object) -> "DirectivePayload":
        selected = _typed_fields(
            value,
            schema=DIRECTIVE_SCHEMA,
            expected=frozenset({
                "schema",
                "directive_id",
                "instruction",
                "supersedes_refs",
            }),
        )
        return cls(
            directive_id=_identifier(
                selected["directive_id"],
                label="directive_id",
                pattern=_DIRECTIVE_ID,
            ),
            instruction=_text(
                selected["instruction"],
                label="instruction",
                maximum_bytes=MAXIMUM_INSTRUCTION_BYTES,
            ),
            supersedes_refs=_ref_tuple(
                selected["supersedes_refs"],
                label="supersedes_refs",
                maximum=MAXIMUM_SUPERSEDES_REFS,
            ),
        )


@dataclass(frozen=True, slots=True)
class Sublemma:
    """One part of a decomposition and the worklist task that carries it."""

    statement: str
    admission_ref: str

    def to_value(self) -> dict[str, object]:
        return {"statement": self.statement, "admission_ref": self.admission_ref}

    @classmethod
    def parse(cls, value: object, *, label: str) -> "Sublemma":
        selected = _exact_fields(
            value, label=label, expected=frozenset({"statement", "admission_ref"})
        )
        return cls(
            statement=_text(
                selected["statement"],
                label=f"{label}.statement",
                maximum_bytes=MAXIMUM_STATEMENT_BYTES,
            ),
            admission_ref=_admission_ref(
                selected["admission_ref"], label=f"{label}.admission_ref"
            ),
        )


@dataclass(frozen=True, slots=True)
class DecompositionPayload:
    """A claim that the listed sublemmas jointly suffice for ``target_ref``."""

    target_ref: str
    sublemmas: tuple[Sublemma, ...]

    def to_value(self) -> dict[str, object]:
        return {
            "schema": DECOMPOSITION_SCHEMA,
            "target_ref": self.target_ref,
            "sublemmas": [item.to_value() for item in self.sublemmas],
            "coverage_claim": "SUBLEMMAS_JOINTLY_SUFFICE_FOR_TARGET",
        }

    @classmethod
    def parse(cls, value: object) -> "DecompositionPayload":
        selected = _typed_fields(
            value,
            schema=DECOMPOSITION_SCHEMA,
            expected=frozenset({
                "schema",
                "target_ref",
                "sublemmas",
                "coverage_claim",
            }),
        )
        if selected["coverage_claim"] != "SUBLEMMAS_JOINTLY_SUFFICE_FOR_TARGET":
            _malformed("coverage_claim")
        raw = selected["sublemmas"]
        if type(raw) is not list or not raw:
            _malformed("sublemmas")
        if len(raw) > MAXIMUM_SUBLEMMAS:
            _malformed("sublemmas: too many")
        parsed = tuple(
            Sublemma.parse(item, label=f"sublemmas[{index}]")
            for index, item in enumerate(raw)
        )
        refs = tuple(item.admission_ref for item in parsed)
        if len(refs) != len(set(refs)):
            _malformed("sublemmas: duplicate admission_ref")
        return cls(
            target_ref=_admission_ref(selected["target_ref"], label="target_ref"),
            sublemmas=parsed,
        )


def _typed_fields(
    value: object,
    *,
    schema: str,
    expected: frozenset[str],
) -> Mapping[str, object]:
    """Validate a top-level treatment payload's discriminator and field set.

    ``cited_directive_refs`` is allowed on every treatment payload so the C and
    D arms compose: a worklist record written under central coordination can
    cite the directive it acts on without needing a separate schema.
    """

    if type(value) is not dict:
        _malformed("payload is not an object")
    if value.get("schema") != schema:
        _fail("PAYLOAD_SCHEMA_MISMATCH", str(value.get("schema"))[:128])
    reject_self_asserted_identity(value)
    if CITED_DIRECTIVE_REFS_FIELD in value:
        _ref_tuple(
            value[CITED_DIRECTIVE_REFS_FIELD],
            label=CITED_DIRECTIVE_REFS_FIELD,
            maximum=MAXIMUM_CITED_DIRECTIVE_REFS,
        )
        selected = {
            key: item
            for key, item in value.items()
            if key != CITED_DIRECTIVE_REFS_FIELD
        }
        return _exact_fields(selected, label=schema, expected=expected)
    return _exact_fields(value, label=schema, expected=expected)


PAYLOAD_PARSERS: Mapping[str, Callable[[object], Any]] = {
    TASK_PROPOSAL_SCHEMA: TaskProposalPayload.parse,
    TASK_ADMISSION_SCHEMA: TaskAdmissionPayload.parse,
    TASK_CLAIM_SCHEMA: TaskClaimPayload.parse,
    TASK_RELEASE_SCHEMA: TaskReleasePayload.parse,
    TASK_OUTCOME_SCHEMA: TaskOutcomePayload.parse,
    DIRECTIVE_SCHEMA: DirectivePayload.parse,
    DECOMPOSITION_SCHEMA: DecompositionPayload.parse,
}


def payload_schema(payload: object) -> str | None:
    """Return the treatment schema of a payload, or ``None`` if it is not one."""

    if type(payload) is not dict:
        return None
    schema = payload.get("schema")
    if type(schema) is not str or schema not in AGENDA_TREATMENT_SCHEMAS:
        return None
    return schema


def cited_directive_refs(payload: object) -> tuple[str, ...] | None:
    """Return declared directive citations, or ``None`` when the field is absent.

    An empty tuple and ``None`` mean different things: the first is a record
    that cited nothing, the second a record that never adopted the convention.
    """

    if type(payload) is not dict or CITED_DIRECTIVE_REFS_FIELD not in payload:
        return None
    return _ref_tuple(
        payload[CITED_DIRECTIVE_REFS_FIELD],
        label=CITED_DIRECTIVE_REFS_FIELD,
        maximum=MAXIMUM_CITED_DIRECTIVE_REFS,
    )


def _contribution(
    *,
    kind: str,
    payload: Mapping[str, object],
    title: str,
    body: str,
    problem_ids: Sequence[str],
    parent_refs: Sequence[str],
    artifact_refs: Sequence[str],
    directive_refs: Sequence[str] | None,
) -> ResearchContribution:
    selected: dict[str, object] = dict(payload)
    if directive_refs is not None:
        # Builders normalize author order into the canonical sorted form; the
        # strict parsers still demand that canonical form of anything read back
        # out of the world.
        if isinstance(directive_refs, (str, bytes)) or not isinstance(
            directive_refs, Sequence
        ):
            _malformed(CITED_DIRECTIVE_REFS_FIELD)
        selected[CITED_DIRECTIVE_REFS_FIELD] = list(
            _ref_tuple(
                sorted(
                    directive_refs,
                    key=lambda item: item if type(item) is str else "",
                ),
                label=CITED_DIRECTIVE_REFS_FIELD,
                maximum=MAXIMUM_CITED_DIRECTIVE_REFS,
            )
        )
    reject_self_asserted_identity(selected)
    return ResearchContribution(
        kind=kind,
        title=title,
        body=body,
        problem_ids=problem_ids,
        parent_refs=parent_refs,
        artifact_refs=artifact_refs,
        payload=selected,
    )


def build_treatment_contribution(
    payload: Mapping[str, object],
    *,
    kind: str,
    title: str,
    body: str,
    problem_ids: Sequence[str] = (),
    parent_refs: Sequence[str] = (),
    artifact_refs: Sequence[str] = (),
    directive_refs: Sequence[str] | None = None,
) -> ResearchContribution:
    """Wrap a treatment payload in an identity-free contribution.

    The returned value is an ordinary :class:`ResearchContribution`; only the
    trusted host may bind it to a ``SessionSpec``.  ``kind`` is checked against
    the schema's declared binding, so a treatment record can never smuggle a
    payload onto a kind the experiment did not authorize.
    """

    schema = payload_schema(payload)
    if schema is None:
        _fail("PAYLOAD_SCHEMA_MISMATCH", "not an agenda-treatment payload")
    allowed = TREATMENT_KIND_BINDING[schema]
    if type(kind) is not str or kind not in allowed:
        _fail("RECORD_KIND_NOT_ALLOWED", f"{schema} accepts {sorted(allowed)!r}")
    if schema == TASK_OUTCOME_SCHEMA:
        parsed = TaskOutcomePayload.parse(payload)
        required = OUTCOME_DISPOSITION_KIND[parsed.disposition]
        if kind != required:
            _fail(
                "RECORD_KIND_NOT_ALLOWED",
                f"{parsed.disposition} outcome must ride {required}",
            )
    else:
        PAYLOAD_PARSERS[schema](payload)
    return _contribution(
        kind=kind,
        payload=payload,
        title=title,
        body=body,
        problem_ids=problem_ids,
        parent_refs=parent_refs,
        artifact_refs=artifact_refs,
        directive_refs=directive_refs,
    )


def build_action_contribution(
    *,
    kind: str,
    title: str,
    body: str,
    directive_refs: Sequence[str] | None = None,
    payload: Mapping[str, object] | None = None,
    problem_ids: Sequence[str] = (),
    parent_refs: Sequence[str] = (),
    artifact_refs: Sequence[str] = (),
) -> ResearchContribution:
    """Build an ordinary research record that may carry directive citations.

    This is the C arm's surface for records that are not themselves treatment
    records: a plain ``ATTEMPT`` or ``RESULT`` whose only agenda-relevant field
    is which live directive it acts under.
    """

    if type(kind) is not str or kind not in RESEARCH_KINDS:
        _fail("RECORD_KIND_NOT_ALLOWED", str(kind)[:64])
    return _contribution(
        kind=kind,
        payload={} if payload is None else payload,
        title=title,
        body=body,
        problem_ids=problem_ids,
        parent_refs=parent_refs,
        artifact_refs=artifact_refs,
        directive_refs=directive_refs,
    )
