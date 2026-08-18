from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pmw_platform.experiments.agenda_arm import (
    ALL_SESSIONS,
    ARM_VERDICT_CODES,
    ARMS,
    OUT_OF_ARM_INSTRUMENT,
    AgendaArm,
    AgendaArmConfig,
    AgendaArmError,
    absent_agenda_arm_announcement,
    build_agenda_arm,
    not_configured_agenda_arm_launch_value,
    not_configured_agenda_arm_session_evidence,
)
from pmw_platform.experiments.agenda_observables import (
    agenda_trigger_reading,
    taskification_onset_tick,
    trigger_cooccurrence,
    trigger_onset_tick,
    trigger_series_value,
    trigger_time_series,
    trigger_time_series_from_arm,
    trigger_time_series_from_world,
)
from pmw_platform.experiments.agenda_treatments import (
    DECOMPOSITION_SCHEMA,
    DIRECTIVE_SCHEMA,
    ROUTE_DECLARATION_SCHEMA,
    TASK_ADMISSION_SCHEMA,
    TASK_CLAIM_SCHEMA,
    TASK_OUTCOME_SCHEMA,
    TASK_PROPOSAL_SCHEMA,
    VERDICT_CODES,
    AgendaRoles,
    AgendaSnapshot,
    RouteDeclarationPayload,
    build_treatment_contribution,
    claim_state,
    validate_route_declaration,
)
from pmw_platform.runtime import orchestrator as orchestrator_module
from pmw_platform.runtime.auth import PreparedCohort
from pmw_platform.runtime.contracts import (
    BackendIdentity,
    BackendOutcome,
    StopProof,
)
from pmw_platform.runtime.context import ContextWindowControl
from pmw_platform.runtime.orchestrator import (
    RuntimeLimits,
    RuntimeOrchestrationError,
    run_prepared_cohort,
)
from pmw_platform.runtime.publish import PublicationIdentity
from pmw_platform.runtime.safety import load_named_profile
from pmw_platform.runtime.store import RuntimeStore, RuntimeStoreError
from pmw_platform.sessions import CohortPlan
from pmw_platform.sessions.model import SessionSpec
from pmw_platform.source_lock import load_core_lock
from pmw_platform.world import ResearchContribution
from pmw_platform.world.records import canonical_json


SNAPSHOT = "snapshot/sha256/" + "a" * 64
RECEIPT_REF = "receipt/sha256/" + "0" * 64


# --------------------------------------------------------------------------
# fixtures: an in-memory world, a scripted backend, and one prepared cohort
# --------------------------------------------------------------------------


def _prepared(
    tmp_path: Path,
    *,
    cohort_id: str,
    count: int,
    concurrency: int = 1,
) -> PreparedCohort:
    profile = load_named_profile("research-default")
    core = load_core_lock()
    briefing = b'{"schema":"TEST_BRIEFING_1"}\n'
    plan = CohortPlan.generate(
        cohort_id=cohort_id,
        world_id="math-frontier",
        world_ref="refs/pmw/math-frontier",
        base_snapshot_ref=SNAPSHOT,
        safety_profile=profile.name,
        safety_profile_sha256=profile.sha256,
        core_lock_sha256=core.sha256,
        briefing_sha256=hashlib.sha256(briefing).hexdigest(),
        count=count,
        concurrency=concurrency,
    )
    cohort_root = tmp_path / "runs" / cohort_id
    cohort_root.mkdir(parents=True)
    return PreparedCohort(
        data_root=tmp_path,
        cohort_root=cohort_root,
        plan_path=cohort_root / "plan.json",
        briefing_path=cohort_root / "briefing.json",
        briefing_bytes=briefing,
        plan=plan,
        profile=profile,
        core_lock=core,
        registration=SimpleNamespace(
            name="math-frontier",
            repo=str(tmp_path / "world.git"),
            world_ref="refs/pmw/math-frontier",
        ),
        world=SimpleNamespace(),
        artifact_store=SimpleNamespace(exists=lambda _ref: True),
    )


class _MemoryPublisher:
    """An in-memory admission log with content-derived admission refs."""

    def __init__(self) -> None:
        self.identity = PublicationIdentity(
            mode="MEMORY_TEST",
            protocol="TEST_PUBLICATION_1",
            public_config={"implementation": "tests"},
        )
        self.rows: list[tuple[str, dict[str, object]]] = []

    def __call__(self, spec, contribution):  # type: ignore[no-untyped-def]
        record = contribution.bind(spec)
        digest = record.content_sha256
        admission_ref = f"admission/sha256/{digest}"
        self.rows.append((admission_ref, record.to_value()))
        return {
            "admission_ref": admission_ref,
            "base_snapshot_ref": spec.base_snapshot_ref,
            "snapshot_ref": SNAPSHOT,
            "receipt_ref": RECEIPT_REF,
            "content_sha256": digest,
        }

    def ref_where(self, schema: str, **fields: object) -> str:
        for admission_ref, content in self.rows:
            payload = content.get("payload")
            if type(payload) is not dict or payload.get("schema") != schema:
                continue
            if all(payload.get(key) == value for key, value in fields.items()):
                return admission_ref
        raise AssertionError(f"no admitted {schema} matching {fields!r}")


class _ScriptedHandle:
    def __init__(self, outcome: BackendOutcome) -> None:
        self.outcome = outcome

    async def wait(self) -> BackendOutcome:
        await asyncio.sleep(0)
        return self.outcome

    async def stop(self, reason: str, grace_seconds: float) -> StopProof:
        del grace_seconds
        return StopProof(stopped=True, reason=reason, process_group_id=None)


class _ScriptedBackend:
    """A backend whose contributions are written when the session starts.

    Building them at start time is what a real session does: it can only cite
    admissions that already exist, so a peer's records are visible only after
    that peer settled.
    """

    def __init__(self, script) -> None:  # type: ignore[no-untyped-def]
        self._identity = BackendIdentity(
            name="scripted-runtime",
            protocol="FAKE_RUNTIME_1",
            public_config={"implementation": "tests"},
        )
        self.script = script
        self.invocations: dict[str, dict[str, object]] = {}

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    @property
    def context_window_control(self) -> ContextWindowControl:
        return ContextWindowControl.NATIVE_MODEL_WINDOW

    def verify_runtime(self) -> None:
        return None

    async def start(self, request):  # type: ignore[no-untyped-def]
        self.invocations[request.spec.session_id] = json.loads(
            Path(request.invocation_path).read_bytes().decode("utf-8")
        )
        contributions = tuple(self.script(request.spec.session_id))
        return _ScriptedHandle(
            BackendOutcome(
                success=True,
                terminal_reason="COMPLETED",
                summary="scripted outcome",
                contributions=contributions,
            )
        )


def _run(prepared, backend, publisher, arm):  # type: ignore[no-untyped-def]
    return asyncio.run(
        run_prepared_cohort(
            prepared,
            backend,
            limits=RuntimeLimits(startup_seconds=5.0, session_wall_seconds=30.0),
            publisher=publisher,
            verifier_kit=None,
            agenda_arm=arm,
        )
    )


# --------------------------------------------------------------------------
# record builders
# --------------------------------------------------------------------------


def _spec(session_id: str, *, cohort_id: str) -> SessionSpec:
    """A frozen identity for a session of some earlier, already dead cohort."""

    return SessionSpec(
        session_id=session_id,
        cohort_id=cohort_id,
        world_id="math-frontier",
        world_ref="refs/pmw/math-frontier",
        base_snapshot_ref=SNAPSHOT,
        safety_profile="research-default",
        safety_profile_sha256="f" * 64,
        core_lock_sha256="c" * 64,
        briefing_sha256="e" * 64,
    )


def _contract(detail: str = "a declared statement settles this task") -> dict:
    return {
        "contract_kind": "DECLARED_STATEMENT",
        "target_id": None,
        "detail": detail,
    }


def _proposal(statement: str) -> ResearchContribution:
    return build_treatment_contribution(
        {
            "schema": TASK_PROPOSAL_SCHEMA,
            "statement": statement,
            "dependency_task_refs": [],
            "completion_contract": _contract(),
        },
        kind="NEED",
        title="proposal",
        body=statement,
    )


def _admission(statement: str) -> ResearchContribution:
    return build_treatment_contribution(
        {
            "schema": TASK_ADMISSION_SCHEMA,
            "statement": statement,
            "dependency_task_refs": [],
            "completion_contract": _contract(),
            "proposal_ref": None,
        },
        kind="CHECKPOINT",
        title="admission",
        body=statement,
    )


def _claim(task_ref: str, *, lease_ticks: int = 100) -> ResearchContribution:
    return build_treatment_contribution(
        {
            "schema": TASK_CLAIM_SCHEMA,
            "task_ref": task_ref,
            "lease_ticks": lease_ticks,
        },
        kind="NOTE",
        title="claim",
        body="claiming one admitted task",
    )


def _directive(directive_id: str = "d1") -> ResearchContribution:
    return build_treatment_contribution(
        {
            "schema": DIRECTIVE_SCHEMA,
            "directive_id": directive_id,
            "instruction": "work the analytic route first",
            "supersedes_refs": [],
        },
        kind="CHECKPOINT",
        title="directive",
        body="coordinator instruction",
    )


def _route(
    statement: str,
    *,
    trigger_refs: tuple[str, ...] = (),
    note: str | None = None,
) -> ResearchContribution:
    return build_treatment_contribution(
        {
            "schema": ROUTE_DECLARATION_SCHEMA,
            "route_statement": statement,
            "peer_trigger_refs": sorted(trigger_refs),
            "differentiation_note": note,
        },
        kind="ATTEMPT",
        title="route",
        body=statement,
    )


def _decomposition(target_ref: str, sublemmas) -> ResearchContribution:  # type: ignore[no-untyped-def]
    return build_treatment_contribution(
        {
            "schema": DECOMPOSITION_SCHEMA,
            "target_ref": target_ref,
            "sublemmas": [
                {"statement": statement, "admission_ref": reference}
                for statement, reference in sublemmas
            ],
            "coverage_claim": "SUBLEMMAS_JOINTLY_SUFFICE_FOR_TARGET",
        },
        kind="RESULT",
        title="decomposition",
        body="the parts jointly suffice",
    )


def _note(body: str = "plain advisory speech") -> ResearchContribution:
    return ResearchContribution(kind="NOTE", title="note", body=body)


def _receipts(result) -> dict[str, dict]:  # type: ignore[no-untyped-def]
    return {
        str(receipt["session_id"]): receipt for receipt in result.receipts
    }


def _codes(receipt) -> list[str]:  # type: ignore[no-untyped-def]
    return [row["code"] for row in receipt["agenda_arm"]["decisions"]]


# --------------------------------------------------------------------------
# D1 -- arm configuration
# --------------------------------------------------------------------------


def test_each_arm_exposes_its_declared_instrument_set() -> None:
    exposures = {
        arm: AgendaArmConfig(arm=arm).instruments for arm in ARMS
    }
    assert exposures == {
        "P": ("advisory",),
        "D": ("binding",),
        "A": ("advisory", "binding"),
        "C": ("advisory", "directive"),
    }
    # Route telemetry is legal everywhere, so route measurement is not itself
    # part of the treatment.
    for arm in ARMS:
        assert (
            ROUTE_DECLARATION_SCHEMA
            in AgendaArmConfig(arm=arm).admitted_payload_schemas
        )
    assert (
        TASK_CLAIM_SCHEMA
        not in AgendaArmConfig(arm="P").admitted_payload_schemas
    )
    assert DIRECTIVE_SCHEMA in AgendaArmConfig(arm="C").admitted_payload_schemas
    assert (
        DIRECTIVE_SCHEMA not in AgendaArmConfig(arm="A").admitted_payload_schemas
    )


def test_d_arm_defaults_to_open_admission_with_no_initializer_slot() -> None:
    config = AgendaArmConfig(arm="D", require_claim_for_primary_action=True)
    assert config.open_admission is True
    assert config.admitting_slots == ALL_SESSIONS
    roles = config.roles(["c-1", "c-2", "c-3"])
    assert roles.admitting_session_ids == ("c-1", "c-2", "c-3")
    assert roles.coordinator_session_ids == ()
    value = config.launch_value()
    assert value["admitting_slots"] == ALL_SESSIONS
    assert value["require_claim_for_primary_action"] is True
    assert value["enforcement"].startswith("PUBLICATION_TIME")  # type: ignore[union-attr]


def test_arm_configuration_rejects_unknown_arms_and_foreign_slots() -> None:
    with pytest.raises(AgendaArmError) as unknown:
        AgendaArmConfig(arm="X")
    assert unknown.value.code == "UNKNOWN_AGENDA_ARM"

    config = AgendaArmConfig(arm="C", coordinator_session_ids=("outsider",))
    with pytest.raises(AgendaArmError) as foreign:
        config.roles(["c-1", "c-2"])
    assert foreign.value.code == "AGENDA_SLOT_NOT_IN_COHORT"

    with pytest.raises(AgendaArmError) as citation:
        AgendaArmConfig(arm="D", enforce_directive_citation=True)
    assert citation.value.code == "MALFORMED_AGENDA_ARM"

    # A coordinator slot on an arm with no directive instrument would be a
    # frozen, permanently unusable authority assignment.
    with pytest.raises(AgendaArmError) as slot:
        AgendaArmConfig(arm="A", coordinator_session_ids=("c-1",))
    assert slot.value.code == "MALFORMED_AGENDA_ARM"


def test_arm_verdict_codes_extend_the_plugin_vocabulary_by_exactly_one() -> None:
    assert ARM_VERDICT_CODES - VERDICT_CODES == {OUT_OF_ARM_INSTRUMENT}


def test_runtime_literals_equal_the_producing_module_constants() -> None:
    # The session runtime describes an arm's absence without importing an
    # experiment; these two definitions must not drift.
    assert (
        orchestrator_module.not_configured_agenda_arm_launch_value()
        == not_configured_agenda_arm_launch_value()
    )
    assert (
        orchestrator_module.not_configured_agenda_arm_session_evidence()
        == not_configured_agenda_arm_session_evidence()
    )
    assert (
        orchestrator_module.absent_agenda_arm_announcement()
        == absent_agenda_arm_announcement()
    )


# --------------------------------------------------------------------------
# D2/D7 -- mini-cohort runs for P, D and A
# --------------------------------------------------------------------------


def test_p_arm_admits_speech_and_telemetry_and_refuses_the_worklist(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-p", count=1)
    publisher = _MemoryPublisher()
    backend = _ScriptedBackend(
        lambda _session_id: (
            _note("an ordinary advisory record"),
            _route("chase the analytic estimate"),
            _proposal("bound the remainder term"),
            _admission("bound the remainder term"),
        )
    )
    arm = AgendaArm(
        AgendaArmConfig(arm="P"),
        session_ids=[spec.session_id for spec in prepared.plan.sessions],
    )

    result = _run(prepared, backend, publisher, arm)

    assert result.outcome == "SUCCEEDED"
    receipt = _receipts(result)["arm-p-session-0001"]
    evidence = receipt["agenda_arm"]
    assert evidence["mode"] == "ENFORCED"
    assert evidence["arm"] == "P"
    assert _codes(receipt) == [
        "ACCEPTED",
        "ACCEPTED",
        OUT_OF_ARM_INSTRUMENT,
        OUT_OF_ARM_INSTRUMENT,
    ]
    assert evidence["reviewed"] == 4
    assert evidence["admitted"] == 2
    assert evidence["rejected"] == 2
    # A rejected instrument is a research event: the session still succeeded
    # and the two admitted records really landed.
    assert receipt["status"] == "SUCCEEDED"
    assert len(receipt["publications"]) == 2
    assert len(publisher.rows) == 2
    assert evidence["route_declarations"]["count"] == 1


def test_d_arm_conflicts_within_a_life_and_releases_at_settlement(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-d", count=3)
    publisher = _MemoryPublisher()

    def script(session_id: str):  # type: ignore[no-untyped-def]
        if session_id.endswith("0001"):
            # Open admission: no initializer role, so an ordinary session puts
            # its own private signal on the worklist.  The directive is out of
            # arm under D.
            return (_admission("bound the remainder term"), _directive())
        task_ref = publisher.ref_where(
            TASK_ADMISSION_SCHEMA, statement="bound the remainder term"
        )
        if session_id.endswith("0002"):
            return (_claim(task_ref), _claim(task_ref, lease_ticks=50))
        return (
            _claim(task_ref),
            _route("continue the remainder bound", trigger_refs=(task_ref,)),
        )

    backend = _ScriptedBackend(script)
    arm = AgendaArm(
        AgendaArmConfig(arm="D", require_claim_for_primary_action=True),
        session_ids=[spec.session_id for spec in prepared.plan.sessions],
    )

    result = _run(prepared, backend, publisher, arm)

    assert result.outcome == "SUCCEEDED"
    receipts = _receipts(result)
    assert _codes(receipts["arm-d-session-0001"]) == [
        "ACCEPTED",
        OUT_OF_ARM_INSTRUMENT,
    ]
    # The holder is alive during its own publication batch, so its second
    # claim on the same task is refused.
    assert _codes(receipts["arm-d-session-0002"]) == [
        "ACCEPTED",
        "TASK_CLAIM_CONFLICT",
    ]
    # After that holder settled, the same task is claimable again.
    third = receipts["arm-d-session-0003"]
    assert _codes(third) == ["ACCEPTED", "ACCEPTED"]
    assert "released at holder settlement" in third["agenda_arm"]["decisions"][0]["detail"]

    released = receipts["arm-d-session-0002"]["agenda_arm"]["lease_release"]
    assert len(released["released_claim_refs"]) == 1
    assert released["released_at_tick"] == 2
    assert third["agenda_arm"]["route_declarations"] == {
        "count": 1,
        "with_peer_trigger_refs": 1,
        "resolved_peer_trigger_refs": 1,
        "dangling_rejected": 0,
        "differentiation_notes": 0,
    }
    assert arm.now_tick == 4
    assert arm.base_tick == 0


def test_at_epoch_equals_lifetime_no_session_can_close_a_lease(
    tmp_path: Path,
) -> None:
    """A structural consequence of epoch = lifetime, recorded rather than hidden.

    A session's own records are admitted at its settlement, so it never learns
    its own claim's admission ref and can never write the ``TaskOutcome`` that
    would close it.  A peer cannot close it either -- chain of custody requires
    the outcome's author to be the claim's holder.  Task completion is
    therefore unreachable until a live read plane exists, which is exactly why
    the settlement-release rule is load-bearing rather than a convenience.
    """

    prepared = _prepared(tmp_path, cohort_id="arm-close", count=3)
    publisher = _MemoryPublisher()

    def script(session_id: str):  # type: ignore[no-untyped-def]
        if session_id.endswith("0001"):
            return (_admission("a task nobody can finish"),)
        task_ref = publisher.ref_where(
            TASK_ADMISSION_SCHEMA, statement="a task nobody can finish"
        )
        if session_id.endswith("0002"):
            return (_claim(task_ref),)
        claim_ref = publisher.ref_where(TASK_CLAIM_SCHEMA, task_ref=task_ref)
        return (
            build_treatment_contribution(
                {
                    "schema": TASK_OUTCOME_SCHEMA,
                    "task_ref": task_ref,
                    "claim_ref": claim_ref,
                    "disposition": "COMPLETED",
                    "completion_evidence": {
                        "contract_kind": "DECLARED_STATEMENT",
                        "detail": "the peer's task looks done to me",
                    },
                },
                kind="RESULT",
                title="outcome",
                body="closing a peer's lease",
            ),
        )

    backend = _ScriptedBackend(script)
    arm = AgendaArm(
        AgendaArmConfig(arm="D"),
        session_ids=[spec.session_id for spec in prepared.plan.sessions],
    )

    result = _run(prepared, backend, publisher, arm)

    assert _codes(_receipts(result)["arm-close-session-0003"]) == [
        "CLAIM_NOT_HELD_BY_AUTHOR"
    ]


def test_a_arm_admits_both_instrument_families(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-a", count=2)
    publisher = _MemoryPublisher()

    def script(session_id: str):  # type: ignore[no-untyped-def]
        if session_id.endswith("0001"):
            return (
                _note("an ordinary advisory record"),
                _admission("bound the remainder term"),
            )
        task_ref = publisher.ref_where(
            TASK_ADMISSION_SCHEMA, statement="bound the remainder term"
        )
        return (
            _claim(task_ref),
            _route(
                "differentiated from the peer's route",
                trigger_refs=(task_ref,),
                note="the peer took the analytic route, so this one is spectral",
            ),
            _directive(),
        )

    backend = _ScriptedBackend(script)
    arm = AgendaArm(
        AgendaArmConfig(arm="A"),
        session_ids=[spec.session_id for spec in prepared.plan.sessions],
    )

    result = _run(prepared, backend, publisher, arm)

    receipts = _receipts(result)
    assert _codes(receipts["arm-a-session-0001"]) == ["ACCEPTED", "ACCEPTED"]
    second = receipts["arm-a-session-0002"]
    assert _codes(second) == ["ACCEPTED", "ACCEPTED", OUT_OF_ARM_INSTRUMENT]
    assert second["agenda_arm"]["instrument_attempts"] == {
        "advisory": 1,
        "binding": 1,
        "directive": 1,
    }
    assert second["agenda_arm"]["route_declarations"]["differentiation_notes"] == 1
    assert second["agenda_arm"]["records_by_schema"] == {
        TASK_CLAIM_SCHEMA: 1,
        ROUTE_DECLARATION_SCHEMA: 1,
        DIRECTIVE_SCHEMA: 1,
    }


def test_c_arm_admits_directives_only_from_the_coordinator_slot(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-c", count=2)
    publisher = _MemoryPublisher()
    backend = _ScriptedBackend(
        lambda _session_id: (_directive(), _admission("a worklist task"))
    )
    arm = AgendaArm(
        AgendaArmConfig(
            arm="C", coordinator_session_ids=("arm-c-session-0001",)
        ),
        session_ids=[spec.session_id for spec in prepared.plan.sessions],
    )

    result = _run(prepared, backend, publisher, arm)

    receipts = _receipts(result)
    assert _codes(receipts["arm-c-session-0001"]) == [
        "ACCEPTED",
        OUT_OF_ARM_INSTRUMENT,
    ]
    assert _codes(receipts["arm-c-session-0002"]) == [
        "NOT_A_COORDINATOR_SLOT",
        OUT_OF_ARM_INSTRUMENT,
    ]


def test_restricted_admitting_slots_reject_a_foreign_admission(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-slots", count=2)
    publisher = _MemoryPublisher()
    backend = _ScriptedBackend(lambda _session_id: (_admission("a task"),))
    arm = AgendaArm(
        AgendaArmConfig(
            arm="D", admitting_slots=("arm-slots-session-0001",)
        ),
        session_ids=[spec.session_id for spec in prepared.plan.sessions],
    )

    result = _run(prepared, backend, publisher, arm)

    receipts = _receipts(result)
    assert _codes(receipts["arm-slots-session-0001"]) == ["ACCEPTED"]
    assert _codes(receipts["arm-slots-session-0002"]) == ["NOT_AN_ADMITTING_SLOT"]


def test_no_arm_configured_publishes_everything_and_says_so(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-none", count=1)
    publisher = _MemoryPublisher()
    backend = _ScriptedBackend(
        lambda _session_id: (_note(), _claim("admission/sha256/" + "c" * 64))
    )

    result = _run(prepared, backend, publisher, None)

    receipt = _receipts(result)["arm-none-session-0001"]
    assert receipt["agenda_arm"] == not_configured_agenda_arm_session_evidence()
    # An unconfigured launch is not the P arm: nothing was validated, and a
    # claim on a nonexistent task was published as written.
    assert len(receipt["publications"]) == 2
    store = RuntimeStore(prepared.cohort_root)
    assert store.read_launch()["agenda_arm"]["mode"] == "NOT_CONFIGURED"
    invocation = backend.invocations["arm-none-session-0001"]
    assert invocation["agenda_arm"] == absent_agenda_arm_announcement()


# --------------------------------------------------------------------------
# D4 -- briefing surface
# --------------------------------------------------------------------------


def test_concurrent_sessions_keep_separate_ledgers_and_one_tick_counter(
    tmp_path: Path,
) -> None:
    """Four sessions, two at a time, over one shared arm ledger."""

    prepared = _prepared(
        tmp_path, cohort_id="arm-parallel", count=4, concurrency=2
    )
    publisher = _MemoryPublisher()
    backend = _ScriptedBackend(
        lambda session_id: (_note(f"speech from {session_id}"), _directive())
    )
    arm = AgendaArm(
        AgendaArmConfig(arm="P"),
        session_ids=[spec.session_id for spec in prepared.plan.sessions],
    )

    result = _run(prepared, backend, publisher, arm)

    assert result.outcome == "SUCCEEDED"
    for receipt in result.receipts:
        assert receipt["status"] == "SUCCEEDED"
        evidence = receipt["agenda_arm"]
        assert (evidence["reviewed"], evidence["admitted"], evidence["rejected"]) == (
            2,
            1,
            1,
        )
        # Ordinals restart per session: one ledger per session, no bleed.
        assert [row["ordinal"] for row in evidence["decisions"]] == [1, 2]
        assert _codes(receipt) == ["ACCEPTED", OUT_OF_ARM_INSTRUMENT]
    # One counter for the whole world: four admissions, four ticks.
    assert arm.now_tick == 4
    assert len(publisher.rows) == 4
    assert len({ref for ref, _content in arm.admissions()}) == 4
    assert arm.settled_session_ids() == frozenset(
        spec.session_id for spec in prepared.plan.sessions
    )


def test_the_invocation_announces_the_instrument_set_without_advice(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-brief", count=1)
    publisher = _MemoryPublisher()
    backend = _ScriptedBackend(lambda _session_id: ())
    arm = AgendaArm(
        AgendaArmConfig(arm="D"),
        session_ids=[spec.session_id for spec in prepared.plan.sessions],
    )

    _run(prepared, backend, publisher, arm)

    announcement = backend.invocations["arm-brief-session-0001"]["agenda_arm"]
    assert announcement["configured"] is True
    assert announcement["arm"] == "D"
    assert announcement["claiming"]["available"] is True
    assert announcement["claiming"]["open_admission"] is True
    assert announcement["route_declaration"]["legal_under_every_arm"] is True
    # It describes the instruments and nothing about which route to take.
    assert set(announcement) == {
        "schema",
        "configured",
        "arm",
        "instruments",
        "admitted_payload_schemas",
        "record_shapes",
        "claiming",
        "route_declaration",
        "statements",
    }
    prose = " ".join(announcement["statements"])
    assert "recommends no route, no ordering and no success criterion" in prose
    assert "using none of them is a permitted outcome" in prose
    assert "not a session failure" in prose
    # Epoch = lifetime is stated rather than left to be discovered.
    assert "only in a later briefing" in prose


def test_a_p_arm_briefing_states_that_no_claiming_instrument_exists(
    tmp_path: Path,
) -> None:
    arm = AgendaArm(AgendaArmConfig(arm="P"), session_ids=["s-1"])
    announcement = arm.briefing_announcement()
    assert announcement["claiming"] == {
        "available": False,
        "reason": "this arm exposes no binding worklist instrument",
    }
    del tmp_path


# --------------------------------------------------------------------------
# D3 -- agenda clock and lease lifecycle
# --------------------------------------------------------------------------


def _fixture_world_claim(lease_ticks: int) -> tuple[str, list[tuple[str, dict]]]:
    """Return a task ref and a base world holding one claim from a past life."""

    past = _spec("past-cohort-session-0001", cohort_id="past-cohort")
    admission = _admission("an inherited task").bind(past)
    task_ref = "admission/sha256/" + admission.content_sha256
    claim = _claim(task_ref, lease_ticks=lease_ticks).bind(past)
    claim_ref = "admission/sha256/" + claim.content_sha256
    return task_ref, [
        (task_ref, admission.to_value()),
        (claim_ref, claim.to_value()),
    ]


def test_pre_launch_admissions_get_the_opening_tick_as_an_upper_bound() -> None:
    _task_ref, rows = _fixture_world_claim(lease_ticks=100)
    arm = AgendaArm(
        AgendaArmConfig(arm="D"),
        session_ids=["c-1"],
        base_records=[(ref, content) for ref, content in rows],
    )
    snapshot = arm.snapshot()
    assert arm.base_tick == 2
    assert arm.now_tick == 2
    assert all(entry.observed_at_tick == 2 for entry in snapshot.entries)
    # An inherited lease looks as young as possible, so it never expires early.
    assert claim_state(snapshot, rows[1][0]) == "LIVE"


def test_a_lease_from_a_previous_lifetime_expires_once_ttl_ticks_elapse() -> None:
    _task_ref, rows = _fixture_world_claim(lease_ticks=2)
    arm = AgendaArm(
        AgendaArmConfig(arm="D"),
        session_ids=["c-1"],
        base_records=list(rows),
    )
    claim_ref = rows[1][0]
    assert claim_state(arm.snapshot(), claim_ref) == "LIVE"

    spec = SimpleNamespace(session_id="c-1")
    for ordinal in range(2):
        arm.observe(
            spec,
            None,
            {
                "admission_ref": f"admission/sha256/{ordinal:064d}",
                "content_sha256": None,
            },
        )
    assert arm.now_tick == 4
    assert claim_state(arm.snapshot(), claim_ref) == "EXPIRED"


def test_a_claim_held_by_a_session_outside_this_launch_never_blocks() -> None:
    task_ref, rows = _fixture_world_claim(lease_ticks=1_000)
    arm = AgendaArm(
        AgendaArmConfig(arm="D"),
        session_ids=["c-1"],
        base_records=list(rows),
    )
    decision = arm.review(SimpleNamespace(session_id="c-1"), _claim(task_ref))
    # Its holder is dead by construction: session IDs are never reused.
    assert decision.code == "ACCEPTED"
    assert "released at holder settlement" in decision.detail


def test_settlement_is_idempotent_and_records_the_release_tick() -> None:
    prepared_ids = ["c-1"]
    arm = AgendaArm(AgendaArmConfig(arm="D"), session_ids=prepared_ids)
    spec = SimpleNamespace(session_id="c-1")
    assert arm.settle(spec) == ()
    assert arm.settle(spec) == ()
    assert arm.settled_session_ids() == frozenset({"c-1"})
    evidence = arm.session_evidence(spec)
    assert evidence["lease_release"]["released_at_tick"] == 0
    assert evidence["agenda_clock"]["base_tick"] == 0


def test_an_arm_that_misses_a_planned_session_is_rejected_read_only(
    tmp_path: Path,
) -> None:
    """The rejection must land before any runtime exists.

    An arm built for the wrong session set would otherwise raise while a
    receipt is being assembled -- after the launch is durable and with no
    settlement to fall back on.
    """

    prepared = _prepared(tmp_path, cohort_id="arm-mismatch", count=2)
    publisher = _MemoryPublisher()
    backend = _ScriptedBackend(lambda _session_id: ())
    arm = AgendaArm(
        AgendaArmConfig(arm="A"),
        session_ids=["arm-mismatch-session-0001"],
    )

    with pytest.raises(RuntimeOrchestrationError) as caught:
        _run(prepared, backend, publisher, arm)

    assert caught.value.code == "AGENDA_ARM_SESSION_SET_MISMATCH"
    assert not (prepared.cohort_root / "runtime").exists()


def test_an_arm_refuses_a_session_outside_its_launch() -> None:
    arm = AgendaArm(AgendaArmConfig(arm="A"), session_ids=["c-1"])
    with pytest.raises(AgendaArmError) as caught:
        arm.review(SimpleNamespace(session_id="c-2"), _note())
    assert caught.value.code == "SESSION_NOT_IN_LAUNCH"


def test_build_agenda_arm_reads_the_world_once_for_its_opening_tick() -> None:
    _task_ref, rows = _fixture_world_claim(lease_ticks=5)
    world = SimpleNamespace(
        records=lambda snapshot_ref=None: tuple(
            SimpleNamespace(admission_ref=ref, content=content)
            for ref, content in rows
        )
    )
    arm = build_agenda_arm(
        AgendaArmConfig(arm="D"), session_ids=["c-1"], world=world
    )
    assert arm.base_tick == 2
    assert len(arm.admissions()) == 2


# --------------------------------------------------------------------------
# D5 -- route telemetry
# --------------------------------------------------------------------------


def test_a_route_declaration_with_a_dangling_trigger_ref_is_refused(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-route", count=2)
    publisher = _MemoryPublisher()

    def script(session_id: str):  # type: ignore[no-untyped-def]
        if session_id.endswith("0001"):
            return (_admission("a real task"),)
        task_ref = publisher.ref_where(
            TASK_ADMISSION_SCHEMA, statement="a real task"
        )
        return (
            _route("cited a peer", trigger_refs=(task_ref,), note="differs"),
            _route("cited nothing that exists", trigger_refs=("admission/sha256/" + "f" * 64,)),
        )

    backend = _ScriptedBackend(script)
    arm = AgendaArm(
        AgendaArmConfig(arm="A"),
        session_ids=[spec.session_id for spec in prepared.plan.sessions],
    )

    result = _run(prepared, backend, publisher, arm)

    receipt = _receipts(result)["arm-route-session-0002"]
    assert _codes(receipt) == ["ACCEPTED", "ROUTE_TRIGGER_REF_UNKNOWN"]
    assert receipt["agenda_arm"]["route_declarations"] == {
        "count": 2,
        "with_peer_trigger_refs": 2,
        "resolved_peer_trigger_refs": 1,
        "dangling_rejected": 1,
        "differentiation_notes": 1,
    }
    assert len(receipt["publications"]) == 1


def test_route_declaration_payload_round_trips_and_rides_attempt_only() -> None:
    payload = RouteDeclarationPayload.parse(
        _route("statement", trigger_refs=("admission/sha256/" + "1" * 64,)).payload
    )
    assert payload.differentiation_note is None
    assert payload.to_value()["schema"] == ROUTE_DECLARATION_SCHEMA

    from pmw_platform.experiments.agenda_treatments import AgendaSchemaError

    with pytest.raises(AgendaSchemaError) as caught:
        build_treatment_contribution(
            {
                "schema": ROUTE_DECLARATION_SCHEMA,
                "route_statement": "s",
                "peer_trigger_refs": [],
                "differentiation_note": None,
            },
            kind="RESULT",
            title="t",
            body="b",
        )
    assert caught.value.code == "RECORD_KIND_NOT_ALLOWED"


def test_a_route_declaration_needs_no_role_and_no_author_identity() -> None:
    snapshot = AgendaSnapshot.build([])
    verdict = validate_route_declaration(snapshot, _route("any route"))
    assert verdict.accepted


# --------------------------------------------------------------------------
# D6 -- analysis-time trigger observable
# --------------------------------------------------------------------------


def _admitted(contribution: ResearchContribution, session_id: str):  # type: ignore[no-untyped-def]
    record = contribution.bind(_spec(session_id, cohort_id="fixture-cohort"))
    return "admission/sha256/" + record.content_sha256, record.to_value()


def _trigger_fixture_world() -> tuple[list[tuple[str, dict]], str, AgendaRoles]:
    target = _admitted(_note("the target theorem"), "fixture-cohort-session-0001")
    first = _admitted(
        _admission("part one"), "fixture-cohort-session-0001"
    )
    second = _admitted(
        _admission("part two"), "fixture-cohort-session-0001"
    )
    decomposition = _admitted(
        _decomposition(
            target[0],
            (("part one", first[0]), ("part two", second[0])),
        ),
        "fixture-cohort-session-0002",
    )
    rows = [target, first, second, decomposition]
    roles = AgendaRoles(admitting_session_ids=("fixture-cohort-session-0001",))
    return rows, target[0], roles


def test_the_trigger_series_dates_onset_by_the_worlds_admission_counter() -> None:
    rows, target_ref, roles = _trigger_fixture_world()

    series = trigger_time_series(rows, target_ref, roles=roles)

    assert [sample.tick for sample in series] == [0, 1, 2, 3, 4]
    assert [sample.fired for sample in series] == [
        False,
        False,
        False,
        False,
        True,
    ]
    assert trigger_onset_tick(series) == 4
    assert taskification_onset_tick(series) == 2
    summary = trigger_cooccurrence(series, target_ref=target_ref)
    assert summary["taskification_lead_ticks"] == 2
    assert summary["non_monotone"] is False
    assert summary["authority"] == "ANALYSIS_TIME_OBSERVABLE_NEVER_A_CONTROL_INPUT"
    assert len(trigger_series_value(series, target_ref=target_ref)["samples"]) == 5


def test_an_objection_unfires_the_trigger_and_the_series_says_so() -> None:
    rows, target_ref, roles = _trigger_fixture_world()
    objection = _admitted(
        ResearchContribution(
            kind="OBJECTION",
            title="objection",
            body="the parts do not cover the target",
            parent_refs=[rows[3][0]],
        ),
        "fixture-cohort-session-0003",
    )
    rows.append(objection)

    series = trigger_time_series(rows, target_ref, roles=roles)

    assert [sample.fired for sample in series[-2:]] == [True, False]
    summary = trigger_cooccurrence(series, target_ref=target_ref)
    assert summary["non_monotone"] is True
    assert summary["final_reading"] is False
    reading = agenda_trigger_reading(rows, target_ref, roles=roles)
    assert reading["fired"] is False
    assert reading["semantics"].startswith("STRUCTURAL_NOT_MATHEMATICAL")  # type: ignore[union-attr]


def test_replaying_an_arm_ledger_skips_its_unordered_base_prefix() -> None:
    """The base prefix is hash-ordered, so it must never be walked as time.

    ``world.records()`` sorts by admission ref, so the pre-launch records in an
    arm's ledger carry no recoverable admission order.  Walking them one at a
    time invents ticks; the arm itself dates all of them at the opening tick.
    """

    rows, target_ref, roles = _trigger_fixture_world()
    arm = AgendaArm(
        AgendaArmConfig(arm="D", admitting_slots=("c-1",)),
        session_ids=["c-1"],
        base_records=list(rows),
    )

    replayed = trigger_time_series_from_arm(arm, target_ref, roles=roles)

    assert [sample.tick for sample in replayed] == [arm.base_tick]
    assert replayed[0].admission_ref is None
    assert replayed[0].fired is True
    # The naive call fabricates one tick per pre-launch record instead.
    naive = trigger_time_series(arm.admissions(), target_ref, roles=roles)
    assert [sample.tick for sample in naive] == [0, 1, 2, 3, 4]


def test_the_trigger_series_can_walk_an_explicit_snapshot_sequence() -> None:
    rows, target_ref, roles = _trigger_fixture_world()
    snapshots = {
        "snapshot/sha256/" + "1" * 64: rows[:3],
        "snapshot/sha256/" + "2" * 64: rows,
    }
    world = SimpleNamespace(
        records=lambda snapshot_ref: tuple(
            SimpleNamespace(admission_ref=ref, content=content)
            for ref, content in snapshots[snapshot_ref]
        )
    )

    series = trigger_time_series_from_world(
        world, list(snapshots), target_ref, roles=roles
    )

    assert [(sample.tick, sample.fired) for sample in series] == [
        (3, False),
        (4, True),
    ]


def test_the_trigger_observable_is_absent_from_the_publication_path() -> None:
    from pmw_platform.experiments import agenda_arm as arm_module

    source = Path(arm_module.__file__).read_text(encoding="utf-8")
    assert "agenda_hardening_trigger" not in source
    assert "settled_decomposition_refs" not in source


# --------------------------------------------------------------------------
# launch and receipt binding
# --------------------------------------------------------------------------


def test_the_launch_binds_the_arm_and_the_receipt_binds_to_the_launch(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-bind", count=1)
    publisher = _MemoryPublisher()
    backend = _ScriptedBackend(lambda _session_id: (_note(),))
    config = AgendaArmConfig(arm="A")
    arm = AgendaArm(
        config, session_ids=[spec.session_id for spec in prepared.plan.sessions]
    )

    result = _run(prepared, backend, publisher, arm)

    store = RuntimeStore(prepared.cohort_root)
    launch = store.read_launch()
    assert launch["agenda_arm"] == config.launch_value()
    assert launch["agenda_arm_sha256"] == config.sha256 == arm.sha256
    receipt = _receipts(result)["arm-bind-session-0001"]
    assert receipt["agenda_arm"]["arm_sha256"] == launch["agenda_arm_sha256"]


def test_the_store_refuses_receipt_evidence_bound_to_another_arm(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-tamper", count=1)
    publisher = _MemoryPublisher()
    backend = _ScriptedBackend(lambda _session_id: (_note(),))
    arm = AgendaArm(
        AgendaArmConfig(arm="A"),
        session_ids=[spec.session_id for spec in prepared.plan.sessions],
    )
    result = _run(prepared, backend, publisher, arm)

    store = RuntimeStore(prepared.cohort_root)
    written = _receipts(result)["arm-tamper-session-0001"]
    evidence = written["agenda_arm"]

    forged_binding = dict(written)
    forged_binding["agenda_arm"] = {**evidence, "arm_sha256": "d" * 64}
    with pytest.raises(RuntimeStoreError) as caught:
        store.write_receipt("arm-tamper-session-0001", forged_binding)
    assert caught.value.code == "MALFORMED_SESSION_RECEIPT"

    # The counts must stay closed: reviewed = admitted + rejected.
    forged_counts = dict(written)
    forged_counts["agenda_arm"] = {**evidence, "rejected": 5}
    with pytest.raises(RuntimeStoreError):
        store.write_receipt("arm-tamper-session-0001", forged_counts)

    # A rejection now excuses a missing publication, so an invented rejection
    # must not close the accounting over a dropped record: the count has to be
    # backed by verdicts that are actually rejections.
    unbacked = dict(written)
    unbacked["agenda_arm"] = {
        **evidence,
        "reviewed": 2,
        "admitted": 1,
        "rejected": 1,
        "verdicts": {"ACCEPTED": 2},
        "decisions": [
            evidence["decisions"][0],  # type: ignore[index]
            {**evidence["decisions"][0], "ordinal": 2},  # type: ignore[index]
        ],
    }
    with pytest.raises(RuntimeStoreError) as unbacked_error:
        store.write_receipt("arm-tamper-session-0001", unbacked)
    assert unbacked_error.value.code == "MALFORMED_SESSION_RECEIPT"

    # The genuine receipt is still the one on disk.
    assert store.read_receipt("arm-tamper-session-0001") == written


def test_a_publisher_that_admits_other_bytes_is_recorded_as_a_divergence(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-diverge", count=1)

    class _LyingPublisher(_MemoryPublisher):
        def __call__(self, spec, contribution):  # type: ignore[no-untyped-def]
            result = super().__call__(spec, contribution)
            return {**result, "content_sha256": "0" * 64}

    publisher = _LyingPublisher()
    backend = _ScriptedBackend(lambda _session_id: (_note(),))
    arm = AgendaArm(
        AgendaArmConfig(arm="A"),
        session_ids=[spec.session_id for spec in prepared.plan.sessions],
    )

    result = _run(prepared, backend, publisher, arm)

    receipt = _receipts(result)["arm-diverge-session-0001"]
    assert receipt["agenda_arm"]["publication_divergences"] == 1
    # The admission still occupies its tick, but opaquely: it can satisfy no
    # treatment rule, because the host never validated those bytes.
    assert arm.now_tick == 1
    assert arm.snapshot().entries[0].is_research_record is False


def test_the_c_arm_can_require_a_live_directive_citation(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-cite", count=2)
    publisher = _MemoryPublisher()

    def script(session_id: str):  # type: ignore[no-untyped-def]
        if session_id.endswith("0001"):
            return (_directive(),)
        directive_ref = publisher.ref_where(DIRECTIVE_SCHEMA, directive_id="d1")
        return (
            ResearchContribution(
                kind="RESULT",
                title="cited",
                body="acting under the directive",
                payload={"cited_directive_refs": [directive_ref]},
            ),
            ResearchContribution(
                kind="RESULT", title="uncited", body="acting under nothing"
            ),
            # OBJECTION is never gated on a directive: a treatment that could
            # silence dissent would corrupt the evidence it measures.
            ResearchContribution(
                kind="OBJECTION", title="objection", body="this route is wrong"
            ),
        )

    backend = _ScriptedBackend(script)
    arm = AgendaArm(
        AgendaArmConfig(
            arm="C",
            coordinator_session_ids=("arm-cite-session-0001",),
            enforce_directive_citation=True,
        ),
        session_ids=[spec.session_id for spec in prepared.plan.sessions],
    )

    result = _run(prepared, backend, publisher, arm)

    assert _codes(_receipts(result)["arm-cite-session-0002"]) == [
        "ACCEPTED",
        "DIRECTIVE_CITATION_MISSING",
        "ACCEPTED",
    ]


def test_session_start_routes_a_selected_arm_into_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pmw_platform import cli

    cohort_id = "cohort-arm"
    _task_ref, rows = _fixture_world_claim(lease_ticks=9)
    prepared = SimpleNamespace(
        cohort_root=tmp_path / "runs" / cohort_id,
        plan=SimpleNamespace(
            cohort_id=cohort_id,
            sessions=(
                SimpleNamespace(session_id=f"{cohort_id}-session-0001"),
                SimpleNamespace(session_id=f"{cohort_id}-session-0002"),
            ),
        ),
        world=SimpleNamespace(
            records=lambda snapshot_ref=None: tuple(
                SimpleNamespace(admission_ref=ref, content=content)
                for ref, content in rows
            )
        ),
    )
    observed: dict[str, object] = {}
    emitted: list[object] = []

    async def fake_run(*_args: object, **kwargs: object) -> object:
        observed["agenda_arm"] = kwargs["agenda_arm"]
        return SimpleNamespace(
            launch_sha256="a" * 64,
            settlement_sha256="b" * 64,
            outcome="SUCCEEDED",
            settlement={"counts": {"SUCCEEDED": 1}},
        )

    monkeypatch.setattr(cli, "authenticate_plan_bundle", lambda *_args: prepared)
    monkeypatch.setattr(cli, "load_command_backend", lambda _path: object())
    monkeypatch.setattr(cli, "run_prepared_cohort", fake_run)
    monkeypatch.setattr(cli, "_runtime_verifier_kit", lambda *_args: None)
    monkeypatch.setattr(
        cli,
        "_runtime_preflight_report",
        lambda *_args: SimpleNamespace(ready=True),
    )
    monkeypatch.setattr(cli, "_emit", lambda value, **_kwargs: emitted.append(value))

    exit_code = cli.main([
        "--data-root",
        str(tmp_path),
        "session",
        "start",
        "--cohort",
        cohort_id,
        "--backend",
        "command",
        "--backend-config",
        str(tmp_path / "backend.json"),
        "--agenda-arm",
        "D",
    ])

    assert exit_code == 0
    arm = observed["agenda_arm"]
    assert arm.arm == "D"
    # The opening tick is the world's admission count, read once at launch.
    assert arm.base_tick == len(rows)
    assert arm.config.require_claim_for_primary_action is True
    assert arm.config.open_admission is True
    assert emitted[0]["agenda_arm"] == "D"  # type: ignore[index]
    assert emitted[0]["agenda_arm_sha256"] == arm.sha256  # type: ignore[index]


def test_the_launch_validator_refuses_a_forged_or_foreign_arm_block(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-launch", count=2)
    publisher = _MemoryPublisher()
    backend = _ScriptedBackend(lambda _session_id: ())
    arm = AgendaArm(
        AgendaArmConfig(arm="D"),
        session_ids=[spec.session_id for spec in prepared.plan.sessions],
    )
    _run(prepared, backend, publisher, arm)
    launch = RuntimeStore(prepared.cohort_root).read_launch()
    session_ids = [spec.session_id for spec in prepared.plan.sessions]

    def _create(value: dict[str, object]) -> None:
        root = tmp_path / f"fresh-{len(list(tmp_path.iterdir()))}"
        root.mkdir()
        RuntimeStore(root).create_launch(value, session_ids=session_ids)

    forged_digest = {**launch, "agenda_arm_sha256": "b" * 64}
    with pytest.raises(RuntimeStoreError) as digest_error:
        _create(forged_digest)
    assert digest_error.value.code == "MALFORMED_RUNTIME_LAUNCH"

    # A slot must name a session this launch actually runs.
    block = {
        **launch["agenda_arm"],  # type: ignore[dict-item]
        "open_admission": False,
        "admitting_slots": ["outsider"],
    }
    foreign = {
        **launch,
        "agenda_arm": block,
        "agenda_arm_sha256": hashlib.sha256(canonical_json(block)).hexdigest(),
    }
    with pytest.raises(RuntimeStoreError) as slot_error:
        _create(foreign)
    assert slot_error.value.code == "MALFORMED_RUNTIME_LAUNCH"


def test_a_launch_written_before_this_wp_is_rejected_fail_closed(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, cohort_id="arm-legacy", count=1)
    publisher = _MemoryPublisher()
    backend = _ScriptedBackend(lambda _session_id: ())
    _run(prepared, backend, publisher, None)

    launch = RuntimeStore(prepared.cohort_root).read_launch()
    del launch["agenda_arm"]
    del launch["agenda_arm_sha256"]

    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    with pytest.raises(RuntimeStoreError) as caught:
        RuntimeStore(legacy_root).create_launch(
            launch,
            session_ids=[spec.session_id for spec in prepared.plan.sessions],
        )
    assert caught.value.code == "MALFORMED_RUNTIME_LAUNCH"
