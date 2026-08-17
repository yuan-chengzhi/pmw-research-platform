"""Agenda-treatment plugin: record schemas and pure validators.

This is an **experiment plugin**, not runtime core.  It defines the record
shapes and decision procedures three agenda treatments need, and nothing else:

* **D arm (worklist)** -- ``TaskProposal``, ``TaskAdmission``, ``TaskClaim``,
  ``TaskRelease`` and ``TaskOutcome``, with an exclusive lease that has a TTL.
* **C arm (central)** -- ``Directive`` from a designated coordinator slot, plus
  the rule that a primary action record cites a live directive.
* **Adaptive arm** -- ``DecompositionRecord`` and the deterministic
  :func:`agenda_hardening_trigger`.

What this module deliberately does not do
-----------------------------------------
* It adds **no seventh record kind**.  Every treatment record is an ordinary
  ``ResearchContribution`` on one of the six existing kinds, discriminated by a
  typed payload schema and a declared kind binding.
* It performs **no orchestration**.  Nothing here starts a session, mutates a
  world, publishes an admission, or is imported by the runtime.  A host that
  wants a treatment enforced must call these validators itself.
* It makes **no model, network or subprocess call**, and reads no clock.  Every
  validator is a pure function of ``(snapshot, candidate)`` plus explicit
  host-supplied arguments.
* It **verifies no mathematics**.  Completion-contract checks are shape and
  provenance checks; the authoritative verifier path is unchanged.
"""

from __future__ import annotations

from .adaptive import (
    MINIMUM_SUBLEMMAS_FOR_TRIGGER,
    agenda_hardening_trigger,
    settled_decomposition_refs,
    validate_decomposition,
)
from .candidates import parse_candidate, require_contribution
from .central import (
    directives,
    live_directive_refs,
    superseded_directive_refs,
    validate_directive,
    validate_directive_citation,
)
from .schemas import (
    AGENDA_TREATMENT_SCHEMAS,
    ARTIFACT_BACKED_CONTRACT_KINDS,
    CITED_DIRECTIVE_REFS_FIELD,
    COMPLETION_CONTRACT_KINDS,
    DECOMPOSITION_SCHEMA,
    DIRECTIVE_SCHEMA,
    HOST_INJECTED_IDENTITY_KEYS,
    OUTCOME_DISPOSITION_KIND,
    OUTCOME_DISPOSITIONS,
    PRIMARY_ACTION_KINDS,
    TASK_ADMISSION_SCHEMA,
    TASK_CLAIM_SCHEMA,
    TASK_OUTCOME_SCHEMA,
    TASK_PROPOSAL_SCHEMA,
    TASK_RELEASE_SCHEMA,
    TREATMENT_KIND_BINDING,
    AgendaSchemaError,
    CompletionContract,
    CompletionEvidence,
    DecompositionPayload,
    DirectivePayload,
    Sublemma,
    TaskAdmissionPayload,
    TaskClaimPayload,
    TaskOutcomePayload,
    TaskProposalPayload,
    TaskReleasePayload,
    build_action_contribution,
    build_treatment_contribution,
    cited_directive_refs,
    payload_schema,
    reject_self_asserted_identity,
)
from .snapshot import (
    AgendaEntry,
    AgendaRoles,
    AgendaSnapshot,
    AgendaSnapshotError,
)
from .verdict import ACCEPTED, VERDICT_CODES, Verdict, accept, reject
from .worklist import (
    BLOCKING_CLAIM_STATES,
    CLAIM_STATES,
    CLOSED,
    EXPIRED,
    LIVE,
    UNDECIDABLE,
    UNKNOWN,
    admitted_tasks,
    blocking_claim_refs,
    check_lease_exclusivity,
    claim_state,
    proposals,
    task_is_completed,
    validate_task_admission,
    validate_task_claim,
    validate_task_outcome,
    validate_task_proposal,
    validate_task_release,
)

__all__ = [
    "ACCEPTED",
    "AGENDA_TREATMENT_SCHEMAS",
    "ARTIFACT_BACKED_CONTRACT_KINDS",
    "BLOCKING_CLAIM_STATES",
    "CITED_DIRECTIVE_REFS_FIELD",
    "CLAIM_STATES",
    "CLOSED",
    "COMPLETION_CONTRACT_KINDS",
    "DECOMPOSITION_SCHEMA",
    "DIRECTIVE_SCHEMA",
    "EXPIRED",
    "HOST_INJECTED_IDENTITY_KEYS",
    "LIVE",
    "MINIMUM_SUBLEMMAS_FOR_TRIGGER",
    "OUTCOME_DISPOSITIONS",
    "OUTCOME_DISPOSITION_KIND",
    "PRIMARY_ACTION_KINDS",
    "TASK_ADMISSION_SCHEMA",
    "TASK_CLAIM_SCHEMA",
    "TASK_OUTCOME_SCHEMA",
    "TASK_PROPOSAL_SCHEMA",
    "TASK_RELEASE_SCHEMA",
    "TREATMENT_KIND_BINDING",
    "UNDECIDABLE",
    "UNKNOWN",
    "VERDICT_CODES",
    "AgendaEntry",
    "AgendaRoles",
    "AgendaSchemaError",
    "AgendaSnapshot",
    "AgendaSnapshotError",
    "CompletionContract",
    "CompletionEvidence",
    "DecompositionPayload",
    "DirectivePayload",
    "Sublemma",
    "TaskAdmissionPayload",
    "TaskClaimPayload",
    "TaskOutcomePayload",
    "TaskProposalPayload",
    "TaskReleasePayload",
    "Verdict",
    "accept",
    "admitted_tasks",
    "agenda_hardening_trigger",
    "blocking_claim_refs",
    "build_action_contribution",
    "build_treatment_contribution",
    "check_lease_exclusivity",
    "cited_directive_refs",
    "claim_state",
    "directives",
    "live_directive_refs",
    "parse_candidate",
    "payload_schema",
    "proposals",
    "reject",
    "reject_self_asserted_identity",
    "require_contribution",
    "settled_decomposition_refs",
    "superseded_directive_refs",
    "task_is_completed",
    "validate_decomposition",
    "validate_directive",
    "validate_directive_citation",
    "validate_task_admission",
    "validate_task_claim",
    "validate_task_outcome",
    "validate_task_proposal",
    "validate_task_release",
]
