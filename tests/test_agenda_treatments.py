"""Zero-model tests for the agenda-treatment plugin.

Every test builds a fixture snapshot from literal values.  Nothing here opens a
world, starts a session, reads a clock or makes a model, network or subprocess
call: the agenda clock is an explicit argument, and every validator is a pure
function of its inputs.
"""

from __future__ import annotations

import pytest

from pmw_platform.experiments.agenda_treatments import (
    ACCEPTED,
    CITED_DIRECTIVE_REFS_FIELD,
    DECOMPOSITION_SCHEMA,
    DIRECTIVE_SCHEMA,
    MINIMUM_SUBLEMMAS_FOR_TRIGGER,
    PRIMARY_ACTION_KINDS,
    TASK_ADMISSION_SCHEMA,
    TASK_CLAIM_SCHEMA,
    TASK_OUTCOME_SCHEMA,
    TASK_PROPOSAL_SCHEMA,
    TASK_RELEASE_SCHEMA,
    TREATMENT_KIND_BINDING,
    AgendaRoles,
    AgendaSchemaError,
    AgendaSnapshot,
    AgendaSnapshotError,
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
    admitted_tasks,
    agenda_hardening_trigger,
    blocking_claim_refs,
    build_action_contribution,
    build_treatment_contribution,
    check_lease_exclusivity,
    claim_state,
    live_directive_refs,
    settled_decomposition_refs,
    task_is_completed,
    validate_decomposition,
    validate_directive,
    validate_directive_citation,
    validate_task_admission,
    validate_task_claim,
    validate_task_outcome,
    validate_task_proposal,
    validate_task_release,
)
from pmw_platform.experiments.agenda_treatments.worklist import (
    CLOSED,
    EXPIRED,
    LIVE,
    UNDECIDABLE,
    UNKNOWN,
)
from pmw_platform.world.records import RESEARCH_KINDS, ResearchRecord


SNAPSHOT = "snapshot/sha256/" + "1" * 64
HOST = "cohort-x-session-0001"
PEER_A = "cohort-x-session-0002"
PEER_B = "cohort-x-session-0003"

ROLES = AgendaRoles(
    coordinator_session_ids=(HOST,), admitting_session_ids=(HOST,)
)


def ref(tag: str) -> str:
    """Return a distinct, well-formed admission ref for a fixture record."""

    digest = (tag * 64)[:64]
    return "admission/sha256/" + "".join(
        character if character in "0123456789abcdef" else "0"
        for character in digest
    )


TASK_1 = ref("a")
TASK_2 = ref("b")
TASK_3 = ref("c")
CLAIM_1 = ref("d")
CLAIM_2 = ref("e")
DIRECTIVE_1 = ref("f")
DIRECTIVE_2 = ref("1")
TARGET = ref("2")
DECOMP = ref("3")
PROPOSAL = ref("4")
OTHER = ref("5")


def record_value(
    session_id: str,
    kind: str,
    payload: dict[str, object] | None = None,
    *,
    parent_refs: tuple[str, ...] = (),
    artifact_refs: tuple[str, ...] = (),
) -> dict[str, object]:
    """Return the stored content of one bound record, as a snapshot holds it."""

    return ResearchRecord(
        world_id="math-frontier",
        cohort_id="cohort-x",
        session_id=session_id,
        base_snapshot_ref=SNAPSHOT,
        kind=kind,
        title="fixture title",
        body="fixture body",
        parent_refs=parent_refs,
        artifact_refs=artifact_refs,
        payload=payload or {},
    ).to_value()


def contract(kind: str = "PEER_CHECKPOINT") -> dict[str, object]:
    return CompletionContract(
        contract_kind=kind, target_id=None, detail="settled when reviewed"
    ).to_value()


def task_payload(
    statement: str,
    *,
    dependencies: tuple[str, ...] = (),
    contract_kind: str = "PEER_CHECKPOINT",
    proposal_ref: str | None = None,
) -> dict[str, object]:
    return TaskAdmissionPayload(
        statement=statement,
        dependency_task_refs=tuple(sorted(dependencies)),
        completion_contract=CompletionContract.parse(contract(contract_kind)),
        proposal_ref=proposal_ref,
    ).to_value()


def claim_payload(task_ref: str, lease_ticks: int = 50) -> dict[str, object]:
    return TaskClaimPayload(task_ref=task_ref, lease_ticks=lease_ticks).to_value()


def outcome_payload(
    task_ref: str,
    claim_ref: str,
    disposition: str = "COMPLETED",
    *,
    contract_kind: str = "PEER_CHECKPOINT",
) -> dict[str, object]:
    evidence = (
        CompletionEvidence(contract_kind=contract_kind, detail="reviewed")
        if disposition == "COMPLETED"
        else None
    )
    return TaskOutcomePayload(
        task_ref=task_ref,
        claim_ref=claim_ref,
        disposition=disposition,
        completion_evidence=evidence,
    ).to_value()


def task_entry(
    admission_ref: str,
    statement: str,
    *,
    session_id: str = HOST,
    dependencies: tuple[str, ...] = (),
    contract_kind: str = "PEER_CHECKPOINT",
) -> tuple[str, object]:
    return (
        admission_ref,
        record_value(
            session_id,
            "CHECKPOINT",
            task_payload(
                statement, dependencies=dependencies, contract_kind=contract_kind
            ),
        ),
    )


def claim_entry(
    admission_ref: str,
    task_ref: str,
    session_id: str,
    lease_ticks: int = 50,
) -> tuple[str, object]:
    return (
        admission_ref,
        record_value(session_id, "NOTE", claim_payload(task_ref, lease_ticks)),
    )


def new_claim(task_ref: str, lease_ticks: int = 50):
    return build_treatment_contribution(
        claim_payload(task_ref, lease_ticks),
        kind="NOTE",
        title="claim",
        body="taking this task",
    )


# --------------------------------------------------------------------------
# Kind vocabulary: the plugin adds no seventh record kind.
# --------------------------------------------------------------------------


def test_every_treatment_schema_rides_an_existing_record_kind() -> None:
    assert RESEARCH_KINDS == frozenset({
        "NOTE",
        "NEED",
        "ATTEMPT",
        "RESULT",
        "OBJECTION",
        "CHECKPOINT",
    })
    for schema, kinds in TREATMENT_KIND_BINDING.items():
        assert kinds, schema
        assert kinds <= RESEARCH_KINDS, schema
    assert PRIMARY_ACTION_KINDS <= RESEARCH_KINDS


def test_a_payload_cannot_ride_a_kind_outside_its_binding() -> None:
    with pytest.raises(AgendaSchemaError) as caught:
        build_treatment_contribution(
            claim_payload(TASK_1), kind="RESULT", title="t", body="b"
        )
    assert caught.value.code == "RECORD_KIND_NOT_ALLOWED"


def test_completed_outcomes_ride_result_and_abandoned_outcomes_ride_attempt() -> None:
    completed = build_treatment_contribution(
        outcome_payload(TASK_1, CLAIM_1, "COMPLETED"),
        kind="RESULT",
        title="t",
        body="b",
    )
    assert completed.kind == "RESULT"
    abandoned = build_treatment_contribution(
        outcome_payload(TASK_1, CLAIM_1, "ABANDONED"),
        kind="ATTEMPT",
        title="t",
        body="b",
    )
    assert abandoned.kind == "ATTEMPT"
    with pytest.raises(AgendaSchemaError) as caught:
        build_treatment_contribution(
            outcome_payload(TASK_1, CLAIM_1, "COMPLETED"),
            kind="ATTEMPT",
            title="t",
            body="b",
        )
    assert caught.value.code == "RECORD_KIND_NOT_ALLOWED"


# --------------------------------------------------------------------------
# Identity-injection boundary.
# --------------------------------------------------------------------------


def test_a_payload_may_not_self_assert_a_host_injected_identity() -> None:
    payload = dict(claim_payload(TASK_1))
    payload["session_id"] = PEER_A
    with pytest.raises(AgendaSchemaError) as caught:
        TaskClaimPayload.parse(payload)
    assert caught.value.code == "IDENTITY_FIELD_SELF_ASSERTED"


def test_a_nested_identity_field_is_rejected_at_any_depth() -> None:
    payload = dict(task_payload("S"))
    payload["completion_contract"] = {
        "contract_kind": "PEER_CHECKPOINT",
        "target_id": None,
        "detail": "d",
        "claimant_session_id": PEER_A,
    }
    with pytest.raises(AgendaSchemaError) as caught:
        TaskAdmissionPayload.parse(payload)
    assert caught.value.code == "IDENTITY_FIELD_SELF_ASSERTED"


def test_a_claim_payload_carries_no_claimant_and_no_start_time() -> None:
    payload = claim_payload(TASK_1)
    assert set(payload) == {"schema", "task_ref", "lease_ticks"}


def test_an_already_bound_record_is_not_a_valid_candidate() -> None:
    snapshot = AgendaSnapshot.build([task_entry(TASK_1, "S1")], now_tick=10)
    bound = new_claim(TASK_1).bind(
        _spec()
    )
    verdict = validate_task_claim(snapshot, bound, roles=ROLES)
    assert verdict.code == "CANDIDATE_NOT_IDENTITY_FREE"


def _spec():
    from pmw_platform.sessions.model import SessionSpec

    return SessionSpec(
        session_id=PEER_A,
        cohort_id="cohort-x",
        world_id="math-frontier",
        world_ref="refs/pmw/research-world",
        base_snapshot_ref=SNAPSHOT,
        safety_profile="research-default",
        safety_profile_sha256="a" * 64,
        core_lock_sha256="b" * 64,
        briefing_sha256="c" * 64,
    )


# --------------------------------------------------------------------------
# Snapshot projection.
# --------------------------------------------------------------------------


def test_snapshot_rejects_duplicate_admissions_and_orphan_clock_entries() -> None:
    with pytest.raises(AgendaSnapshotError):
        AgendaSnapshot.build(
            [task_entry(TASK_1, "S1"), task_entry(TASK_1, "S2")], now_tick=1
        )
    with pytest.raises(AgendaSnapshotError):
        AgendaSnapshot.build(
            [task_entry(TASK_1, "S1")], now_tick=1, observed_at_ticks={OTHER: 3}
        )


def test_non_platform_admissions_stay_opaque_rather_than_breaking_the_view() -> None:
    snapshot = AgendaSnapshot.build(
        [
            task_entry(TASK_1, "S1"),
            (TARGET, {"schema": "PMW_FRONTIER_TARGET_CARD_1", "target_id": "aim-60"}),
            (OTHER, "not json at all"),
        ],
        now_tick=5,
    )
    assert len(snapshot) == 3
    assert snapshot.get(TARGET) is not None
    assert snapshot.get(TARGET).is_research_record is False
    assert snapshot.malformed_admission_refs == ()
    assert list(admitted_tasks(snapshot, ROLES)) == [TASK_1]


def test_a_record_claiming_the_platform_schema_but_invalid_is_flagged() -> None:
    snapshot = AgendaSnapshot.build(
        [(OTHER, {"schema": "PMW_RESEARCH_RECORD_1", "kind": "NOTE"})], now_tick=1
    )
    assert snapshot.malformed_admission_refs == (OTHER,)
    assert admitted_tasks(snapshot, ROLES) == {}


# --------------------------------------------------------------------------
# D arm: the worklist itself.
# --------------------------------------------------------------------------


def test_only_a_designated_admitting_slot_puts_a_task_on_the_worklist() -> None:
    snapshot = AgendaSnapshot.build(
        [
            task_entry(TASK_1, "S1", session_id=HOST),
            task_entry(TASK_2, "S2", session_id=PEER_A),
        ],
        now_tick=10,
    )
    assert list(admitted_tasks(snapshot, ROLES)) == [TASK_1]

    candidate = build_treatment_contribution(
        task_payload("S3"), kind="CHECKPOINT", title="t", body="b"
    )
    assert (
        validate_task_admission(
            snapshot, candidate, roles=ROLES, prospective_session_id=HOST
        ).code
        == ACCEPTED
    )
    assert (
        validate_task_admission(
            snapshot, candidate, roles=ROLES, prospective_session_id=PEER_A
        ).code
        == "NOT_AN_ADMITTING_SLOT"
    )
    assert (
        validate_task_admission(snapshot, candidate, roles=ROLES).code
        == "AUTHOR_IDENTITY_REQUIRED"
    )


def test_a_self_admitted_task_cannot_then_be_claimed() -> None:
    snapshot = AgendaSnapshot.build(
        [task_entry(TASK_2, "S2", session_id=PEER_A)], now_tick=10
    )
    verdict = validate_task_claim(snapshot, new_claim(TASK_2), roles=ROLES)
    assert verdict.code == "TASK_UNKNOWN"


def test_any_peer_may_propose_but_dependencies_must_already_exist() -> None:
    snapshot = AgendaSnapshot.build([task_entry(TASK_1, "S1")], now_tick=10)
    good = build_treatment_contribution(
        TaskProposalPayload(
            statement="new work",
            dependency_task_refs=(TASK_1,),
            completion_contract=CompletionContract.parse(contract()),
        ).to_value(),
        kind="NEED",
        title="t",
        body="b",
    )
    assert validate_task_proposal(snapshot, good, roles=ROLES).code == ACCEPTED

    bad = build_treatment_contribution(
        TaskProposalPayload(
            statement="new work",
            dependency_task_refs=(OTHER,),
            completion_contract=CompletionContract.parse(contract()),
        ).to_value(),
        kind="NEED",
        title="t",
        body="b",
    )
    assert (
        validate_task_proposal(snapshot, bad, roles=ROLES).code
        == "TASK_DEPENDENCY_UNKNOWN"
    )


def test_an_admission_may_only_cite_a_proposal_that_exists() -> None:
    snapshot = AgendaSnapshot.build(
        [
            (
                PROPOSAL,
                record_value(
                    PEER_A,
                    "NEED",
                    TaskProposalPayload(
                        statement="proposed",
                        dependency_task_refs=(),
                        completion_contract=CompletionContract.parse(contract()),
                    ).to_value(),
                ),
            )
        ],
        now_tick=10,
    )
    accepted = build_treatment_contribution(
        task_payload("proposed", proposal_ref=PROPOSAL),
        kind="CHECKPOINT",
        title="t",
        body="b",
    )
    assert (
        validate_task_admission(
            snapshot, accepted, roles=ROLES, prospective_session_id=HOST
        ).code
        == ACCEPTED
    )
    dangling = build_treatment_contribution(
        task_payload("proposed", proposal_ref=OTHER),
        kind="CHECKPOINT",
        title="t",
        body="b",
    )
    assert (
        validate_task_admission(
            snapshot, dangling, roles=ROLES, prospective_session_id=HOST
        ).code
        == "PROPOSAL_UNKNOWN"
    )


# --------------------------------------------------------------------------
# D arm: lease exclusivity, TTL expiry and conflict rejection.
# --------------------------------------------------------------------------


def held_snapshot(now_tick: int | None, *, observed: int | None = 100):
    entries = [
        task_entry(TASK_1, "S1"),
        claim_entry(CLAIM_1, TASK_1, PEER_A, lease_ticks=50),
    ]
    ticks = {} if observed is None else {CLAIM_1: observed}
    return AgendaSnapshot.build(entries, now_tick=now_tick, observed_at_ticks=ticks)


def test_a_live_lease_rejects_a_competing_claim() -> None:
    snapshot = held_snapshot(now_tick=120)
    assert claim_state(snapshot, CLAIM_1) == LIVE
    assert blocking_claim_refs(snapshot, TASK_1, ROLES) == (CLAIM_1,)
    verdict = validate_task_claim(snapshot, new_claim(TASK_1), roles=ROLES)
    assert verdict.code == "TASK_CLAIM_CONFLICT"
    assert CLAIM_1 in verdict.detail


def test_a_lease_expires_exactly_at_its_ttl_boundary() -> None:
    # observed at 100, lease 50: live through tick 149, expired from tick 150.
    assert claim_state(held_snapshot(now_tick=149), CLAIM_1) == LIVE
    assert claim_state(held_snapshot(now_tick=150), CLAIM_1) == EXPIRED
    assert (
        validate_task_claim(
            held_snapshot(now_tick=149), new_claim(TASK_1), roles=ROLES
        ).code
        == "TASK_CLAIM_CONFLICT"
    )
    assert (
        validate_task_claim(
            held_snapshot(now_tick=150), new_claim(TASK_1), roles=ROLES
        ).code
        == ACCEPTED
    )


def test_an_expired_lease_stops_blocking_its_task() -> None:
    snapshot = held_snapshot(now_tick=400)
    assert blocking_claim_refs(snapshot, TASK_1, ROLES) == ()
    assert check_lease_exclusivity(snapshot, ROLES).code == ACCEPTED


def test_the_same_holder_may_not_stack_a_second_live_lease() -> None:
    # Exclusivity is a property of the world, not of who is asking, so even a
    # renewal by the current holder must go through release-then-claim.
    snapshot = held_snapshot(now_tick=120)
    verdict = validate_task_claim(snapshot, new_claim(TASK_1), roles=ROLES)
    assert verdict.code == "TASK_CLAIM_CONFLICT"


def test_a_released_lease_frees_its_task_before_the_ttl_elapses() -> None:
    entries = [
        task_entry(TASK_1, "S1"),
        claim_entry(CLAIM_1, TASK_1, PEER_A, lease_ticks=50),
        (
            ref("6"),
            record_value(
                PEER_A,
                "NOTE",
                TaskReleasePayload(task_ref=TASK_1, claim_ref=CLAIM_1).to_value(),
            ),
        ),
    ]
    snapshot = AgendaSnapshot.build(
        entries, now_tick=120, observed_at_ticks={CLAIM_1: 100}
    )
    assert claim_state(snapshot, CLAIM_1) == CLOSED
    assert blocking_claim_refs(snapshot, TASK_1, ROLES) == ()
    assert validate_task_claim(snapshot, new_claim(TASK_1), roles=ROLES).code == ACCEPTED


def test_a_release_written_by_a_peer_does_not_free_the_lease() -> None:
    entries = [
        task_entry(TASK_1, "S1"),
        claim_entry(CLAIM_1, TASK_1, PEER_A, lease_ticks=50),
        (
            ref("6"),
            record_value(
                PEER_B,
                "NOTE",
                TaskReleasePayload(task_ref=TASK_1, claim_ref=CLAIM_1).to_value(),
            ),
        ),
    ]
    snapshot = AgendaSnapshot.build(
        entries, now_tick=120, observed_at_ticks={CLAIM_1: 100}
    )
    assert claim_state(snapshot, CLAIM_1) == LIVE
    assert validate_task_claim(snapshot, new_claim(TASK_1), roles=ROLES).code == (
        "TASK_CLAIM_CONFLICT"
    )


def test_a_lease_without_a_clock_is_undecidable_and_fails_closed() -> None:
    without_now = held_snapshot(now_tick=None)
    without_stamp = held_snapshot(now_tick=120, observed=None)
    for snapshot in (without_now, without_stamp):
        assert claim_state(snapshot, CLAIM_1) == UNDECIDABLE
        assert blocking_claim_refs(snapshot, TASK_1, ROLES) == (CLAIM_1,)
        verdict = validate_task_claim(snapshot, new_claim(TASK_1), roles=ROLES)
        assert verdict.code == "LEASE_LIVENESS_UNDECIDABLE"


def test_an_unknown_claim_ref_has_no_state() -> None:
    assert claim_state(held_snapshot(now_tick=120), OTHER) == UNKNOWN


def test_exclusivity_audit_catches_two_simultaneously_live_leases() -> None:
    entries = [
        task_entry(TASK_1, "S1"),
        claim_entry(CLAIM_1, TASK_1, PEER_A, lease_ticks=50),
        claim_entry(CLAIM_2, TASK_1, PEER_B, lease_ticks=50),
    ]
    snapshot = AgendaSnapshot.build(
        entries, now_tick=120, observed_at_ticks={CLAIM_1: 100, CLAIM_2: 110}
    )
    assert blocking_claim_refs(snapshot, TASK_1, ROLES) == tuple(
        sorted((CLAIM_1, CLAIM_2))
    )
    verdict = check_lease_exclusivity(snapshot, ROLES)
    assert verdict.code == "TASK_CLAIM_CONFLICT"
    assert TASK_1 in verdict.detail


def test_exclusivity_is_unproven_rather_than_satisfied_without_a_clock() -> None:
    entries = [
        task_entry(TASK_1, "S1"),
        claim_entry(CLAIM_1, TASK_1, PEER_A, lease_ticks=50),
        claim_entry(CLAIM_2, TASK_1, PEER_B, lease_ticks=50),
    ]
    snapshot = AgendaSnapshot.build(entries, now_tick=None)
    assert check_lease_exclusivity(snapshot, ROLES).code == "TASK_CLAIM_CONFLICT"


def test_sequential_leases_on_one_task_satisfy_exclusivity() -> None:
    entries = [
        task_entry(TASK_1, "S1"),
        claim_entry(CLAIM_1, TASK_1, PEER_A, lease_ticks=50),
        claim_entry(CLAIM_2, TASK_1, PEER_B, lease_ticks=50),
    ]
    # CLAIM_1 expired at 150; CLAIM_2 observed at 160 and still live at 170.
    snapshot = AgendaSnapshot.build(
        entries, now_tick=170, observed_at_ticks={CLAIM_1: 100, CLAIM_2: 160}
    )
    assert blocking_claim_refs(snapshot, TASK_1, ROLES) == (CLAIM_2,)
    assert check_lease_exclusivity(snapshot, ROLES).code == ACCEPTED


# --------------------------------------------------------------------------
# D arm: dependency readiness and chain of custody.
# --------------------------------------------------------------------------


def completed_task_entries(contract_kind: str = "PEER_CHECKPOINT"):
    return [
        task_entry(TASK_1, "S1", contract_kind=contract_kind),
        claim_entry(CLAIM_1, TASK_1, PEER_A),
        (
            ref("7"),
            record_value(
                PEER_A,
                "RESULT",
                outcome_payload(
                    TASK_1, CLAIM_1, "COMPLETED", contract_kind=contract_kind
                ),
            ),
        ),
    ]


def test_a_dependency_blocks_a_claim_until_it_is_completed() -> None:
    entries = [
        task_entry(TASK_1, "S1"),
        task_entry(TASK_2, "S2", dependencies=(TASK_1,)),
    ]
    snapshot = AgendaSnapshot.build(entries, now_tick=10)
    assert (
        validate_task_claim(snapshot, new_claim(TASK_2), roles=ROLES).code
        == "TASK_DEPENDENCIES_UNREADY"
    )

    ready = AgendaSnapshot.build(
        completed_task_entries() + [task_entry(TASK_2, "S2", dependencies=(TASK_1,))],
        now_tick=120,
        observed_at_ticks={CLAIM_1: 100},
    )
    assert task_is_completed(ready, TASK_1, ROLES) is True
    assert validate_task_claim(ready, new_claim(TASK_2), roles=ROLES).code == ACCEPTED


def test_a_completed_task_cannot_be_reclaimed() -> None:
    snapshot = AgendaSnapshot.build(
        completed_task_entries(), now_tick=120, observed_at_ticks={CLAIM_1: 100}
    )
    assert (
        validate_task_claim(snapshot, new_claim(TASK_1), roles=ROLES).code
        == "TASK_ALREADY_COMPLETED"
    )


def test_a_peer_cannot_complete_another_sessions_task() -> None:
    entries = [
        task_entry(TASK_1, "S1"),
        claim_entry(CLAIM_1, TASK_1, PEER_A),
        (
            ref("7"),
            record_value(PEER_B, "RESULT", outcome_payload(TASK_1, CLAIM_1)),
        ),
    ]
    snapshot = AgendaSnapshot.build(
        entries, now_tick=120, observed_at_ticks={CLAIM_1: 100}
    )
    assert task_is_completed(snapshot, TASK_1, ROLES) is False
    assert claim_state(snapshot, CLAIM_1) == LIVE


def test_a_dependency_that_is_not_an_admitted_task_is_named_as_unknown() -> None:
    entries = [task_entry(TASK_2, "S2", dependencies=(OTHER,))]
    snapshot = AgendaSnapshot.build(entries, now_tick=10)
    assert (
        validate_task_claim(snapshot, new_claim(TASK_2), roles=ROLES).code
        == "TASK_DEPENDENCY_UNKNOWN"
    )


# --------------------------------------------------------------------------
# D arm: release and outcome authority.
# --------------------------------------------------------------------------


def release_candidate(task_ref: str = TASK_1, claim_ref: str = CLAIM_1):
    return build_treatment_contribution(
        TaskReleasePayload(task_ref=task_ref, claim_ref=claim_ref).to_value(),
        kind="NOTE",
        title="release",
        body="handing it back",
    )


def test_only_the_lease_holder_may_close_its_own_lease() -> None:
    snapshot = held_snapshot(now_tick=120)
    assert (
        validate_task_release(
            snapshot, release_candidate(), roles=ROLES, prospective_session_id=PEER_A
        ).code
        == ACCEPTED
    )
    assert (
        validate_task_release(
            snapshot, release_candidate(), roles=ROLES, prospective_session_id=PEER_B
        ).code
        == "CLAIM_NOT_HELD_BY_AUTHOR"
    )
    assert (
        validate_task_release(snapshot, release_candidate(), roles=ROLES).code
        == "AUTHOR_IDENTITY_REQUIRED"
    )


def test_release_rejects_an_unknown_claim_and_a_task_mismatch() -> None:
    snapshot = AgendaSnapshot.build(
        [
            task_entry(TASK_1, "S1"),
            task_entry(TASK_2, "S2"),
            claim_entry(CLAIM_1, TASK_1, PEER_A),
        ],
        now_tick=120,
        observed_at_ticks={CLAIM_1: 100},
    )
    assert (
        validate_task_release(
            snapshot,
            release_candidate(TASK_1, OTHER),
            roles=ROLES,
            prospective_session_id=PEER_A,
        ).code
        == "CLAIM_UNKNOWN"
    )
    assert (
        validate_task_release(
            snapshot,
            release_candidate(TASK_2, CLAIM_1),
            roles=ROLES,
            prospective_session_id=PEER_A,
        ).code
        == "CLAIM_TASK_MISMATCH"
    )


def test_an_expired_lease_may_still_be_released_but_not_completed() -> None:
    snapshot = held_snapshot(now_tick=400)
    assert (
        validate_task_release(
            snapshot, release_candidate(), roles=ROLES, prospective_session_id=PEER_A
        ).code
        == ACCEPTED
    )
    outcome = build_treatment_contribution(
        outcome_payload(TASK_1, CLAIM_1, "COMPLETED"),
        kind="RESULT",
        title="done",
        body="finished",
    )
    assert (
        validate_task_outcome(
            snapshot, outcome, roles=ROLES, prospective_session_id=PEER_A
        ).code
        == "LEASE_EXPIRED"
    )


def test_a_lease_cannot_be_closed_twice() -> None:
    entries = [
        task_entry(TASK_1, "S1"),
        claim_entry(CLAIM_1, TASK_1, PEER_A),
        (
            ref("6"),
            record_value(
                PEER_A,
                "NOTE",
                TaskReleasePayload(task_ref=TASK_1, claim_ref=CLAIM_1).to_value(),
            ),
        ),
    ]
    snapshot = AgendaSnapshot.build(
        entries, now_tick=120, observed_at_ticks={CLAIM_1: 100}
    )
    assert (
        validate_task_release(
            snapshot, release_candidate(), roles=ROLES, prospective_session_id=PEER_A
        ).code
        == "CLAIM_ALREADY_CLOSED"
    )


def test_an_outcome_must_answer_the_admitted_completion_contract() -> None:
    snapshot = held_snapshot(now_tick=120)
    mismatched = build_treatment_contribution(
        outcome_payload(TASK_1, CLAIM_1, "COMPLETED", contract_kind="DECLARED_STATEMENT"),
        kind="RESULT",
        title="done",
        body="finished",
    )
    assert (
        validate_task_outcome(
            snapshot, mismatched, roles=ROLES, prospective_session_id=PEER_A
        ).code
        == "COMPLETION_CONTRACT_MISMATCH"
    )


def test_an_artifact_backed_contract_requires_an_artifact_ref() -> None:
    entries = [
        task_entry(TASK_1, "S1", contract_kind="AMF_VERIFIER_PASS"),
        claim_entry(CLAIM_1, TASK_1, PEER_A),
    ]
    snapshot = AgendaSnapshot.build(
        entries, now_tick=120, observed_at_ticks={CLAIM_1: 100}
    )
    payload = outcome_payload(
        TASK_1, CLAIM_1, "COMPLETED", contract_kind="AMF_VERIFIER_PASS"
    )
    bare = build_treatment_contribution(
        payload, kind="RESULT", title="done", body="finished"
    )
    assert (
        validate_task_outcome(
            snapshot, bare, roles=ROLES, prospective_session_id=PEER_A
        ).code
        == "COMPLETION_EVIDENCE_MISSING"
    )
    backed = build_treatment_contribution(
        payload,
        kind="RESULT",
        title="done",
        body="finished",
        artifact_refs=("artifact/sha256/" + "a" * 64,),
    )
    assert (
        validate_task_outcome(
            snapshot, backed, roles=ROLES, prospective_session_id=PEER_A
        ).code
        == ACCEPTED
    )


def test_a_completed_outcome_must_carry_evidence_and_others_must_not() -> None:
    with pytest.raises(AgendaSchemaError) as caught:
        TaskOutcomePayload.parse({
            "schema": TASK_OUTCOME_SCHEMA,
            "task_ref": TASK_1,
            "claim_ref": CLAIM_1,
            "disposition": "COMPLETED",
            "completion_evidence": None,
        })
    assert caught.value.code == "COMPLETION_EVIDENCE_MISSING"

    with pytest.raises(AgendaSchemaError) as caught:
        TaskOutcomePayload.parse({
            "schema": TASK_OUTCOME_SCHEMA,
            "task_ref": TASK_1,
            "claim_ref": CLAIM_1,
            "disposition": "ABANDONED",
            "completion_evidence": {
                "contract_kind": "PEER_CHECKPOINT",
                "detail": "d",
            },
        })
    assert caught.value.code == "PAYLOAD_MALFORMED"


# --------------------------------------------------------------------------
# C arm: directives and the citation rule.
# --------------------------------------------------------------------------


def directive_entry(
    admission_ref: str,
    directive_id: str,
    *,
    session_id: str = HOST,
    supersedes: tuple[str, ...] = (),
) -> tuple[str, object]:
    return (
        admission_ref,
        record_value(
            session_id,
            "CHECKPOINT",
            DirectivePayload(
                directive_id=directive_id,
                instruction="work the reduction first",
                supersedes_refs=tuple(sorted(supersedes)),
            ).to_value(),
        ),
    )


def test_only_a_designated_coordinator_slot_issues_a_directive() -> None:
    snapshot = AgendaSnapshot.build([], now_tick=1)
    candidate = build_treatment_contribution(
        DirectivePayload(
            directive_id="d1", instruction="do this", supersedes_refs=()
        ).to_value(),
        kind="CHECKPOINT",
        title="directive",
        body="b",
    )
    assert (
        validate_directive(
            snapshot, candidate, roles=ROLES, prospective_session_id=HOST
        ).code
        == ACCEPTED
    )
    assert (
        validate_directive(
            snapshot, candidate, roles=ROLES, prospective_session_id=PEER_A
        ).code
        == "NOT_A_COORDINATOR_SLOT"
    )
    assert (
        validate_directive(snapshot, candidate, roles=ROLES).code
        == "AUTHOR_IDENTITY_REQUIRED"
    )


def test_a_directive_written_from_a_peer_slot_never_becomes_live() -> None:
    snapshot = AgendaSnapshot.build(
        [directive_entry(DIRECTIVE_1, "d1", session_id=PEER_A)], now_tick=1
    )
    assert live_directive_refs(snapshot, ROLES) == ()


def test_a_later_directive_supersedes_an_earlier_one() -> None:
    snapshot = AgendaSnapshot.build(
        [
            directive_entry(DIRECTIVE_1, "d1"),
            directive_entry(DIRECTIVE_2, "d2", supersedes=(DIRECTIVE_1,)),
        ],
        now_tick=1,
    )
    assert live_directive_refs(snapshot, ROLES) == (DIRECTIVE_2,)


def test_a_directive_may_only_supersede_a_directive_that_exists() -> None:
    snapshot = AgendaSnapshot.build([directive_entry(DIRECTIVE_1, "d1")], now_tick=1)
    candidate = build_treatment_contribution(
        DirectivePayload(
            directive_id="d2", instruction="replace", supersedes_refs=(OTHER,)
        ).to_value(),
        kind="CHECKPOINT",
        title="directive",
        body="b",
    )
    assert (
        validate_directive(
            snapshot, candidate, roles=ROLES, prospective_session_id=HOST
        ).code
        == "SUPERSEDED_DIRECTIVE_UNKNOWN"
    )


def directive_snapshot():
    return AgendaSnapshot.build(
        [
            directive_entry(DIRECTIVE_1, "d1"),
            directive_entry(DIRECTIVE_2, "d2", supersedes=(DIRECTIVE_1,)),
        ],
        now_tick=1,
    )


def test_a_primary_action_record_must_cite_a_live_directive() -> None:
    snapshot = directive_snapshot()
    good = build_action_contribution(
        kind="ATTEMPT", title="t", body="b", directive_refs=(DIRECTIVE_2,)
    )
    assert validate_directive_citation(snapshot, good, roles=ROLES).code == ACCEPTED

    superseded = build_action_contribution(
        kind="ATTEMPT", title="t", body="b", directive_refs=(DIRECTIVE_1,)
    )
    assert (
        validate_directive_citation(snapshot, superseded, roles=ROLES).code
        == "DIRECTIVE_NOT_LIVE"
    )

    both = build_action_contribution(
        kind="ATTEMPT", title="t", body="b", directive_refs=(DIRECTIVE_1, DIRECTIVE_2)
    )
    assert validate_directive_citation(snapshot, both, roles=ROLES).code == ACCEPTED


def test_an_uncited_primary_action_record_is_rejected() -> None:
    snapshot = directive_snapshot()
    missing = build_action_contribution(kind="RESULT", title="t", body="b")
    assert (
        validate_directive_citation(snapshot, missing, roles=ROLES).code
        == "DIRECTIVE_CITATION_MISSING"
    )
    empty = build_action_contribution(
        kind="RESULT", title="t", body="b", directive_refs=()
    )
    assert (
        validate_directive_citation(snapshot, empty, roles=ROLES).code
        == "DIRECTIVE_CITATION_MISSING"
    )


def test_a_citation_of_an_unknown_or_unauthorized_directive_is_rejected() -> None:
    snapshot = directive_snapshot()
    unknown = build_action_contribution(
        kind="ATTEMPT", title="t", body="b", directive_refs=(OTHER,)
    )
    assert (
        validate_directive_citation(snapshot, unknown, roles=ROLES).code
        == "DIRECTIVE_UNKNOWN"
    )

    peer_authored = AgendaSnapshot.build(
        [directive_entry(DIRECTIVE_1, "d1", session_id=PEER_A)], now_tick=1
    )
    citing = build_action_contribution(
        kind="ATTEMPT", title="t", body="b", directive_refs=(DIRECTIVE_1,)
    )
    assert (
        validate_directive_citation(peer_authored, citing, roles=ROLES).code
        == "DIRECTIVE_UNKNOWN"
    )


def test_observations_dissent_and_directives_are_exempt_from_the_citation_rule() -> None:
    snapshot = directive_snapshot()
    for kind in ("NOTE", "NEED", "OBJECTION"):
        candidate = build_action_contribution(kind=kind, title="t", body="b")
        assert validate_directive_citation(snapshot, candidate, roles=ROLES).code == (
            ACCEPTED
        )

    directive = build_treatment_contribution(
        DirectivePayload(
            directive_id="d3", instruction="next", supersedes_refs=()
        ).to_value(),
        kind="CHECKPOINT",
        title="t",
        body="b",
    )
    assert validate_directive_citation(snapshot, directive, roles=ROLES).code == ACCEPTED


def test_a_worklist_record_can_also_carry_a_directive_citation() -> None:
    snapshot = directive_snapshot()
    claim = build_treatment_contribution(
        claim_payload(TASK_1),
        kind="NOTE",
        title="claim",
        body="b",
        directive_refs=(DIRECTIVE_2,),
    )
    assert claim.payload[CITED_DIRECTIVE_REFS_FIELD] == [DIRECTIVE_2]
    # The composed payload still parses as a claim.
    with_task = AgendaSnapshot.build([task_entry(TASK_1, "S1")], now_tick=1)
    assert validate_task_claim(with_task, claim, roles=ROLES).code == ACCEPTED


# --------------------------------------------------------------------------
# Adaptive arm: the hardening trigger.
# --------------------------------------------------------------------------


def decomposition_payload(
    target_ref: str = TARGET,
    sublemmas: tuple[tuple[str, str], ...] = (("S1", TASK_1), ("S2", TASK_2)),
) -> dict[str, object]:
    return DecompositionPayload(
        target_ref=target_ref,
        sublemmas=tuple(
            Sublemma(statement=statement, admission_ref=admission_ref)
            for statement, admission_ref in sublemmas
        ),
    ).to_value()


def trigger_entries(
    *,
    sublemmas: tuple[tuple[str, str], ...] = (("S1", TASK_1), ("S2", TASK_2)),
    decomposition_kind: str = "RESULT",
    target_ref: str = TARGET,
    include_target: bool = True,
    task_author: str = HOST,
):
    entries: list[tuple[str, object]] = [
        task_entry(TASK_1, "S1", session_id=task_author),
        task_entry(TASK_2, "S2", session_id=task_author),
        (
            DECOMP,
            record_value(
                PEER_A,
                decomposition_kind,
                decomposition_payload(target_ref, sublemmas),
            ),
        ),
    ]
    if include_target:
        entries.append(
            (TARGET, {"schema": "PMW_FRONTIER_TARGET_CARD_1", "target_id": "aim-60"})
        )
    return entries


def test_the_trigger_fires_on_a_grounded_unobjected_decomposition() -> None:
    snapshot = AgendaSnapshot.build(trigger_entries(), now_tick=1)
    assert validate_decomposition(
        snapshot,
        build_treatment_contribution(
            decomposition_payload(), kind="RESULT", title="t", body="b"
        ),
        roles=ROLES,
    ).code == ACCEPTED
    assert agenda_hardening_trigger(snapshot, TARGET, roles=ROLES) is True
    assert settled_decomposition_refs(snapshot, TARGET, roles=ROLES) == (DECOMP,)


def test_the_trigger_is_false_without_any_decomposition() -> None:
    snapshot = AgendaSnapshot.build(
        [
            task_entry(TASK_1, "S1"),
            (TARGET, {"schema": "PMW_FRONTIER_TARGET_CARD_1", "target_id": "aim-60"}),
        ],
        now_tick=1,
    )
    assert agenda_hardening_trigger(snapshot, TARGET, roles=ROLES) is False


def test_the_trigger_is_false_when_the_target_is_absent_from_the_snapshot() -> None:
    snapshot = AgendaSnapshot.build(trigger_entries(include_target=False), now_tick=1)
    assert agenda_hardening_trigger(snapshot, TARGET, roles=ROLES) is False
    assert agenda_hardening_trigger(snapshot, OTHER, roles=ROLES) is False


def test_the_trigger_needs_at_least_two_sublemmas() -> None:
    assert MINIMUM_SUBLEMMAS_FOR_TRIGGER == 2
    snapshot = AgendaSnapshot.build(
        trigger_entries(sublemmas=(("S1", TASK_1),)), now_tick=1
    )
    assert agenda_hardening_trigger(snapshot, TARGET, roles=ROLES) is False


def test_the_trigger_is_false_when_a_sublemma_is_not_an_admitted_task() -> None:
    snapshot = AgendaSnapshot.build(
        trigger_entries(sublemmas=(("S1", TASK_1), ("S3", TASK_3))), now_tick=1
    )
    assert agenda_hardening_trigger(snapshot, TARGET, roles=ROLES) is False


def test_the_trigger_is_false_when_sublemma_tasks_lack_admitting_authority() -> None:
    snapshot = AgendaSnapshot.build(
        trigger_entries(task_author=PEER_A), now_tick=1
    )
    assert agenda_hardening_trigger(snapshot, TARGET, roles=ROLES) is False


def test_the_trigger_is_false_when_a_sublemma_statement_was_reworded() -> None:
    snapshot = AgendaSnapshot.build(
        trigger_entries(sublemmas=(("S1", TASK_1), ("reworded", TASK_2))), now_tick=1
    )
    assert agenda_hardening_trigger(snapshot, TARGET, roles=ROLES) is False
    verdict = validate_decomposition(
        snapshot,
        build_treatment_contribution(
            decomposition_payload(sublemmas=(("S1", TASK_1), ("reworded", TASK_2))),
            kind="RESULT",
            title="t",
            body="b",
        ),
        roles=ROLES,
    )
    assert verdict.code == "DECOMPOSITION_STATEMENT_MISMATCH"


def test_the_trigger_is_false_when_the_decomposition_rides_a_wrong_kind() -> None:
    snapshot = AgendaSnapshot.build(
        trigger_entries(decomposition_kind="NOTE"), now_tick=1
    )
    assert agenda_hardening_trigger(snapshot, TARGET, roles=ROLES) is False


def test_an_objection_naming_the_decomposition_unsettles_the_trigger() -> None:
    entries = trigger_entries()
    objected = entries + [
        (ref("8"), record_value(PEER_B, "OBJECTION", {}, parent_refs=(DECOMP,)))
    ]
    snapshot = AgendaSnapshot.build(objected, now_tick=1)
    assert agenda_hardening_trigger(snapshot, TARGET, roles=ROLES) is False
    assert settled_decomposition_refs(snapshot, TARGET, roles=ROLES) == ()


def test_an_objection_naming_something_else_leaves_the_trigger_true() -> None:
    entries = trigger_entries()
    unrelated = entries + [
        (ref("8"), record_value(PEER_B, "OBJECTION", {}, parent_refs=(TASK_1,)))
    ]
    snapshot = AgendaSnapshot.build(unrelated, now_tick=1)
    assert agenda_hardening_trigger(snapshot, TARGET, roles=ROLES) is True


def test_the_trigger_does_not_depend_on_the_agenda_clock() -> None:
    entries = trigger_entries()
    for now_tick in (None, 0, 10_000):
        snapshot = AgendaSnapshot.build(entries, now_tick=now_tick)
        assert agenda_hardening_trigger(snapshot, TARGET, roles=ROLES) is True


def test_the_trigger_targets_exactly_the_requested_target() -> None:
    entries = trigger_entries() + [
        (OTHER, {"schema": "PMW_FRONTIER_TARGET_CARD_1", "target_id": "aim-61"})
    ]
    snapshot = AgendaSnapshot.build(entries, now_tick=1)
    assert agenda_hardening_trigger(snapshot, TARGET, roles=ROLES) is True
    assert agenda_hardening_trigger(snapshot, OTHER, roles=ROLES) is False


def test_a_decomposition_naming_an_absent_target_is_rejected() -> None:
    snapshot = AgendaSnapshot.build([task_entry(TASK_1, "S1")], now_tick=1)
    candidate = build_treatment_contribution(
        decomposition_payload(), kind="RESULT", title="t", body="b"
    )
    assert (
        validate_decomposition(snapshot, candidate, roles=ROLES).code
        == "DECOMPOSITION_TARGET_UNKNOWN"
    )


def test_a_decomposition_sublemma_must_be_an_admitted_task() -> None:
    snapshot = AgendaSnapshot.build(
        [
            task_entry(TASK_1, "S1"),
            (TARGET, {"schema": "PMW_FRONTIER_TARGET_CARD_1", "target_id": "aim-60"}),
        ],
        now_tick=1,
    )
    candidate = build_treatment_contribution(
        decomposition_payload(), kind="RESULT", title="t", body="b"
    )
    assert (
        validate_decomposition(snapshot, candidate, roles=ROLES).code
        == "DECOMPOSITION_SUBLEMMA_UNKNOWN"
    )


# --------------------------------------------------------------------------
# Payload strictness.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"schema": "SOMETHING_ELSE"}, "PAYLOAD_SCHEMA_MISMATCH"),
        ({"schema": TASK_CLAIM_SCHEMA, "task_ref": TASK_1}, "PAYLOAD_MALFORMED"),
        (
            {"schema": TASK_CLAIM_SCHEMA, "task_ref": "not-a-ref", "lease_ticks": 5},
            "PAYLOAD_MALFORMED",
        ),
        (
            {"schema": TASK_CLAIM_SCHEMA, "task_ref": TASK_1, "lease_ticks": 0},
            "PAYLOAD_MALFORMED",
        ),
        (
            {"schema": TASK_CLAIM_SCHEMA, "task_ref": TASK_1, "lease_ticks": True},
            "PAYLOAD_MALFORMED",
        ),
    ],
)
def test_claim_payload_parsing_is_strict(payload: dict, code: str) -> None:
    with pytest.raises(AgendaSchemaError) as caught:
        TaskClaimPayload.parse(payload)
    assert caught.value.code == code


def test_reference_lists_must_be_sorted_and_duplicate_free() -> None:
    unsorted_refs = {
        "schema": TASK_ADMISSION_SCHEMA,
        "statement": "S",
        "dependency_task_refs": [TASK_2, TASK_1],
        "completion_contract": contract(),
        "proposal_ref": None,
    }
    with pytest.raises(AgendaSchemaError) as caught:
        TaskAdmissionPayload.parse(unsorted_refs)
    assert caught.value.code == "PAYLOAD_MALFORMED"

    duplicated = dict(unsorted_refs, dependency_task_refs=[TASK_1, TASK_1])
    with pytest.raises(AgendaSchemaError) as caught:
        TaskAdmissionPayload.parse(duplicated)
    assert caught.value.code == "PAYLOAD_MALFORMED"


def test_every_treatment_payload_round_trips_through_its_parser() -> None:
    values = [
        (
            TASK_PROPOSAL_SCHEMA,
            TaskProposalPayload,
            TaskProposalPayload(
                statement="S",
                dependency_task_refs=(),
                completion_contract=CompletionContract.parse(contract()),
            ).to_value(),
        ),
        (TASK_ADMISSION_SCHEMA, TaskAdmissionPayload, task_payload("S")),
        (TASK_CLAIM_SCHEMA, TaskClaimPayload, claim_payload(TASK_1)),
        (
            TASK_RELEASE_SCHEMA,
            TaskReleasePayload,
            TaskReleasePayload(task_ref=TASK_1, claim_ref=CLAIM_1).to_value(),
        ),
        (TASK_OUTCOME_SCHEMA, TaskOutcomePayload, outcome_payload(TASK_1, CLAIM_1)),
        (
            DIRECTIVE_SCHEMA,
            DirectivePayload,
            DirectivePayload(
                directive_id="d1", instruction="i", supersedes_refs=()
            ).to_value(),
        ),
        (DECOMPOSITION_SCHEMA, DecompositionPayload, decomposition_payload()),
    ]
    for schema, parser, value in values:
        assert value["schema"] == schema
        assert parser.parse(value).to_value() == value


def test_a_verdict_reports_a_stable_code_and_never_a_bare_boolean() -> None:
    verdict = validate_task_claim(
        AgendaSnapshot.build([], now_tick=1), new_claim(TASK_1), roles=ROLES
    )
    assert verdict.code == "TASK_UNKNOWN"
    assert verdict.accepted is False
    assert verdict.to_value() == {
        "accepted": False,
        "code": "TASK_UNKNOWN",
        "detail": TASK_1,
    }
    assert not hasattr(verdict, "__bool__") or type(verdict).__bool__ is object.__bool__
