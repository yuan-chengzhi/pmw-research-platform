from __future__ import annotations

import json
from pathlib import Path

import pytest

from pmw_platform.runtime.contracts import (
    BackendIdentity,
    BackendOutcome,
    RuntimeContractError,
    SessionRequest,
    StopProof,
)
from pmw_platform.sessions import SessionSpec
from pmw_platform.world import ResearchContribution


SHA = "a" * 64
SNAPSHOT = "snapshot/sha256/" + "b" * 64


def _spec() -> SessionSpec:
    return SessionSpec(
        session_id="cohort-a-session-0001",
        cohort_id="cohort-a",
        world_id="math-frontier",
        world_ref="refs/pmw/math-frontier",
        base_snapshot_ref=SNAPSHOT,
        safety_profile="research-default",
        safety_profile_sha256="c" * 64,
        core_lock_sha256="d" * 64,
        briefing_sha256="e" * 64,
    )


def _contribution() -> ResearchContribution:
    return ResearchContribution(
        kind="ATTEMPT",
        problem_ids=("degree-diameter-3-9",),
        title="A route worth checking",
        body="The backend proposes content; the host supplies identity.",
        payload={"confidence": "exploratory"},
    )


def test_backend_identity_is_canonical_public_and_defensively_copied() -> None:
    config = {
        "auth_kind": "oauth",
        "provider": "example",
        "limits": {"context_window": 1_000_000},
    }
    identity = BackendIdentity(
        name="pi-rpc",
        protocol="PI_RPC_1",
        public_config=config,
    )
    same = BackendIdentity(
        name="pi-rpc",
        protocol="PI_RPC_1",
        public_config=json.loads(json.dumps(config)),
    )

    config["limits"]["context_window"] = 1  # type: ignore[index]
    exposed = identity.public_config
    exposed["provider"] = "changed"

    assert identity.public_config["provider"] == "example"
    assert identity.public_config["limits"] == {"context_window": 1_000_000}
    assert identity.sha256 == same.sha256


@pytest.mark.parametrize(
    "secret_key",
    ["access_token", "refresh-token", "api_key", "client_secret", "password"],
)
def test_backend_identity_rejects_nested_secret_bearing_keys(
    secret_key: str,
) -> None:
    with pytest.raises(RuntimeContractError) as caught:
        BackendIdentity(
            name="unsafe",
            protocol="COMMAND_1",
            public_config={"nested": {secret_key: "must-not-cross"}},
        )

    assert caught.value.code == "SECRET_IN_PUBLIC_IDENTITY"


def test_backend_outcome_roundtrips_only_identity_free_contributions() -> None:
    outcome = BackendOutcome(
        success=True,
        terminal_reason="COMPLETED",
        summary="One bounded result",
        contributions=(_contribution(),),
        usage={"input_tokens": 123},
        evidence={"result_sha256": "f" * 64},
    )

    wire = outcome.to_value()
    contribution_wire = wire["contributions"][0]  # type: ignore[index]
    assert set(contribution_wire).isdisjoint(
        {"world_id", "cohort_id", "session_id", "base_snapshot_ref"}
    )
    assert BackendOutcome.from_value(wire).to_value() == wire

    contribution_wire["session_id"] = "forged"  # type: ignore[index]
    with pytest.raises(RuntimeContractError) as caught:
        BackendOutcome.from_value(wire)
    assert caught.value.code == "MALFORMED_BACKEND_OUTCOME"


def test_backend_outcome_copies_metadata_and_applies_utf8_bounds() -> None:
    usage = {"nested": {"calls": 1}}
    outcome = BackendOutcome(
        success=False,
        terminal_reason="RESEARCH_FAILED",
        summary="bounded",
        usage=usage,
    )
    usage["nested"]["calls"] = 99  # type: ignore[index]
    returned = outcome.usage
    returned["nested"] = {}
    assert outcome.usage == {"nested": {"calls": 1}}

    with pytest.raises(RuntimeContractError) as caught:
        BackendOutcome(
            success=True,
            terminal_reason="COMPLETED",
            summary="\N{SNOWMAN}" * 30_000,
        )
    assert caught.value.code == "MALFORMED_BACKEND_OUTCOME"


def test_session_request_requires_authenticated_spec_and_fixed_digests(
    tmp_path: Path,
) -> None:
    paths = [tmp_path / name for name in (
        "briefing.json",
        "invocation.json",
        "private",
        "workspace",
        "cache",
        "evidence",
    )]
    request = SessionRequest(
        plan_sha256=SHA,
        launch_sha256="f" * 64,
        spec=_spec(),
        briefing_path=paths[0],
        invocation_path=paths[1],
        private_root=paths[2],
        workspace=paths[3],
        cache=paths[4],
        evidence=paths[5],
        session_wall_seconds=30.0,
        stop_grace_seconds=1.0,
    )
    assert request.spec.session_id == "cohort-a-session-0001"

    with pytest.raises(TypeError, match="authenticated CohortPlan"):
        SessionRequest(
            plan_sha256=SHA,
            launch_sha256="f" * 64,
            spec=object(),  # type: ignore[arg-type]
            briefing_path=paths[0],
            invocation_path=paths[1],
            private_root=paths[2],
            workspace=paths[3],
            cache=paths[4],
            evidence=paths[5],
            session_wall_seconds=30.0,
            stop_grace_seconds=1.0,
        )

    with pytest.raises(RuntimeContractError) as caught:
        SessionRequest(
            plan_sha256="A" * 64,
            launch_sha256="f" * 64,
            spec=_spec(),
            briefing_path=paths[0],
            invocation_path=paths[1],
            private_root=paths[2],
            workspace=paths[3],
            cache=paths[4],
            evidence=paths[5],
            session_wall_seconds=30.0,
            stop_grace_seconds=1.0,
        )
    assert caught.value.code == "MALFORMED_SESSION_REQUEST"

    with pytest.raises(RuntimeContractError) as caught:
        SessionRequest(
            plan_sha256=SHA,
            launch_sha256="f" * 64,
            spec=_spec(),
            briefing_path=paths[0],
            invocation_path=paths[1],
            private_root=paths[2],
            workspace=paths[3],
            cache=paths[4],
            evidence=paths[5],
            session_wall_seconds=0,
            stop_grace_seconds=1.0,
        )
    assert caught.value.code == "MALFORMED_SESSION_REQUEST"


def test_stop_proof_is_explicit_about_unproven_cleanup() -> None:
    unknown = StopProof(
        stopped=False,
        reason="PROCESS_GROUP_STILL_OBSERVED",
        process_group_id=321,
        detail="Host could not prove that all descendants exited.",
    )
    assert unknown.to_value()["stopped"] is False

    with pytest.raises(RuntimeContractError) as caught:
        StopProof(stopped=True, reason="human prose")
    assert caught.value.code == "MALFORMED_STOP_PROOF"
