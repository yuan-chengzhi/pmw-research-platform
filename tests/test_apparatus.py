from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pmw_platform.apparatus import (
    ApparatusError,
    _audit_amf_source_authority,
    target_bindings_from_briefing,
)
from pmw_platform.source_materializer import MaterializedSource
from pmw_platform.world.records import canonical_json


REGISTRY_SHA256 = "1" * 64
PROBLEM_CARD_SHA256 = "3" * 64


def _fixture(tmp_path: Path) -> tuple[bytes, object]:
    tree = tmp_path / "tree"
    (tree / "data").mkdir(parents=True)
    (tree / "targets" / "fixture").mkdir(parents=True)
    (tree / "verifiers" / "amf.fixture.exact.v1").mkdir(parents=True)
    candidate = {"additionalProperties": False, "type": "object"}
    candidate_raw = json.dumps(candidate, indent=2, sort_keys=True).encode() + b"\n"
    candidate_path = "targets/fixture/candidate.schema.json"
    (tree / candidate_path).write_bytes(candidate_raw)
    candidate_pin = {
        "path": candidate_path,
        "bytes": len(candidate_raw),
        "sha256": hashlib.sha256(candidate_raw).hexdigest(),
    }
    card = {
        "schema": "AMF_TARGET_CARD_1",
        "problem_id": "fixture",
        "problem_card_sha256": PROBLEM_CARD_SHA256,
        "canonical_statement": "Find an exact fixture.",
        "source_revision": "fixture-v1",
        "claim_scope": "FINITE_INSTANCE",
        "success_criterion": "The exact checker accepts.",
        "partial_progress_criterion": "Retain reproducible partial certificates.",
        "stop_conditions": ["The exact target changes."],
        "candidate_schema": candidate_pin,
        "verifier_id": "amf.fixture.exact.v1",
    }
    card_raw = json.dumps(card, indent=2, sort_keys=True).encode() + b"\n"
    card_path = "targets/fixture/target-card.json"
    (tree / card_path).write_bytes(card_raw)
    card_sha256 = hashlib.sha256(card_raw).hexdigest()
    row = {
        "claim_scope": "FINITE_INSTANCE",
        "formalization_level": "executable_spec",
        "hard_gates": {"exact_target": "pass"},
        "problem_card_sha256": PROBLEM_CARD_SHA256,
        "problem_id": "fixture",
        "role": "experimental_active",
        "strict_stage": "curated",
        "target_card": {
            "path": card_path,
            "bytes": len(card_raw),
            "sha256": card_sha256,
        },
        "verification_mode": "synthetic_exact_check",
        "verifier_id": "amf.fixture.exact.v1",
    }
    portfolio = {
        "schema": "AMF_EXPERIMENTAL_PORTFOLIO_1",
        "verifier_registry_sha256": REGISTRY_SHA256,
        "targets": [row],
    }
    (tree / "data" / "experimental-portfolio.json").write_bytes(
        json.dumps(portfolio, indent=2, sort_keys=True).encode() + b"\n"
    )
    checker_path = "verifiers/amf.fixture.exact.v1/check.py"
    checker_raw = b"raise SystemExit(0)\n"
    (tree / checker_path).write_bytes(checker_raw)
    source_artifacts = [
        {
            "path": checker_path,
            "bytes": len(checker_raw),
            "sha256": hashlib.sha256(checker_raw).hexdigest(),
        }
    ]
    manifest_path = "verifiers/amf.fixture.exact.v1/manifest.json"
    manifest = {
        "schema": "AMF_VERIFIER_MANIFEST_1",
        "verifier_id": "amf.fixture.exact.v1",
        "binds_verification_mode": "synthetic_exact_check",
        "source_artifacts": source_artifacts,
    }
    manifest_raw = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    (tree / manifest_path).write_bytes(manifest_raw)
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    problem = {
        "target_id": "fixture",
        "target_card_ref": f"target-card/sha256/{card_sha256}",
        "target_card_sha256": card_sha256,
        "problem_card_sha256": PROBLEM_CARD_SHA256,
        "canonical_statement": "Find an exact fixture.",
        "source_revision": "fixture-v1",
        "claim_scope": "FINITE_INSTANCE",
        "success_condition": "The exact checker accepts.",
        "partial_success_conditions": ["Retain reproducible partial certificates."],
        "stop_conditions": ["The exact target changes."],
        "candidate_contract": {
            "media_type": "application/schema+json",
            "schema": candidate,
            "source_bytes": len(candidate_raw),
            "source_path": candidate_path,
            "source_sha256": candidate_pin["sha256"],
        },
        "formalization_level": "executable_spec",
        "hard_gates": {"exact_target": "pass"},
        "portfolio_role": "experimental_active",
        "research_context": {},
        "risks": [],
        "stage": "experimental_eligible",
        "strict_stage": "curated",
        "verification_mode": "synthetic_exact_check",
        "verification_contract": {
            "kind": "EXACT_EXECUTABLE",
            "registry_path": "data/verifiers.json",
            "registry_sha256": REGISTRY_SHA256,
            "manifest_path": manifest_path,
            "manifest_sha256": manifest_sha256,
            "source_artifacts": source_artifacts,
            "verifier_id": "amf.fixture.exact.v1",
        },
    }
    target_card_admission_ref = f"admission/sha256/{'4' * 64}"
    briefing = {
        "schema": "PMW_MATHEMATICAL_SITUATION_2",
        "world_id": "fixture-world",
        "world_ref": "refs/pmw/fixture-world",
        "snapshot_ref": f"snapshot/sha256/{'5' * 64}",
        "problem_count": 1,
        "admission_count": 1,
        "problems": [
            {
                "problem_id": "fixture",
                "target_card_admission_ref": target_card_admission_ref,
                "target_card_ref": problem["target_card_ref"],
                "problem": problem,
                "omitted_historical_runtime_contract": {
                    "field": "budget_contract",
                    "sha256": "6" * 64,
                    "bytes": 1,
                    "semantics": "NON_OPERATIVE_PREDECESSOR_CAMPAIGN_PROVENANCE",
                    "exact_content": {
                        "method": "world.get",
                        "admission_ref": target_card_admission_ref,
                    },
                },
                "research_admission_refs": [],
            }
        ],
        "records": [],
        "unscoped_admission_refs": [],
        "schema_counts": {},
        "semantics": {
            "problem_cards": "MATHEMATICAL_CONTENT_WITH_PREDECESSOR_RUNTIME_BUDGET_OMITTED",
            "runtime_authority": "HOST_INVOCATION_AND_LAUNCH_ONLY_NOT_HISTORICAL_WORLD_RECORDS",
            "records": "ONE_BOUNDED_LOSS_AWARE_PROJECTION_PER_NON_CARD_ADMISSION",
            "problem_links": "RESEARCH_ADMISSION_REFS_JOIN_TO_RECORDS",
            "truth_ranking": "NONE",
            "exact_record_access": "world.get(admission_ref, snapshot_ref)",
        },
    }
    briefing_raw = canonical_json(briefing) + b"\n"
    materialized = MaterializedSource(
        name="agent-math-frontier",
        repository="https://example.invalid/agent-math-frontier.git",
        commit="a" * 40,
        git_tree="b" * 40,
        tree_sha256="c" * 64,
        file_count=5,
        total_bytes=1,
        root=tmp_path,
        tree_path=tree,
        manifest_path=tmp_path / "manifest.json",
        manifest_sha256="d" * 64,
    )

    class StubMaterializer:
        def audit(self, name: str) -> MaterializedSource:
            assert name == "agent-math-frontier"
            return materialized

    return briefing_raw, StubMaterializer()


def test_briefing_bindings_are_cross_checked_against_locked_target_cards(
    tmp_path: Path,
) -> None:
    briefing, materializer = _fixture(tmp_path)
    bindings = target_bindings_from_briefing(briefing)

    authority = _audit_amf_source_authority(
        briefing_bytes=briefing,
        source_materializer=materializer,  # type: ignore[arg-type]
        bindings=bindings,
        registry_sha256=REGISTRY_SHA256,
    )

    assert authority.target_count == 1
    assert len(authority.portfolio_sha256) == 64
    assert len(authority.target_card_closure_sha256) == 64


def test_world_cannot_restate_a_locked_target_and_keep_the_same_verifier(
    tmp_path: Path,
) -> None:
    briefing, materializer = _fixture(tmp_path)
    value = json.loads(briefing)
    value["problems"][0]["problem"]["canonical_statement"] = "A substituted target."
    substituted = canonical_json(value) + b"\n"
    bindings = target_bindings_from_briefing(substituted)

    with pytest.raises(ApparatusError) as raised:
        _audit_amf_source_authority(
            briefing_bytes=substituted,
            source_materializer=materializer,  # type: ignore[arg-type]
            bindings=bindings,
            registry_sha256=REGISTRY_SHA256,
        )

    assert raised.value.code == "AMF_TARGET_CARD_BRIEFING_MISMATCH"


@pytest.mark.parametrize(
    ("mutation", "detail"),
    [
        (
            lambda value: value["problems"][0]["problem"].__setitem__(
                "verified_solution", "fabricated"
            ),
            "problem_fields",
        ),
        (
            lambda value: value["problems"][0].__setitem__(
                "problem_id", "different-target"
            ),
            "row_identity",
        ),
    ],
)
def test_v2_rejects_problem_envelope_confusion(
    tmp_path: Path, mutation: object, detail: str
) -> None:
    briefing, _materializer = _fixture(tmp_path)
    value = json.loads(briefing)
    mutation(value)  # type: ignore[operator]

    with pytest.raises(ApparatusError) as raised:
        target_bindings_from_briefing(canonical_json(value) + b"\n")

    assert detail in raised.value.detail


def test_v2_accepts_a_fresh_card_without_historical_runtime_contract(
    tmp_path: Path,
) -> None:
    briefing, _materializer = _fixture(tmp_path)
    value = json.loads(briefing)
    value["problems"][0]["omitted_historical_runtime_contract"] = None

    bindings = target_bindings_from_briefing(canonical_json(value) + b"\n")

    assert [binding.target_id for binding in bindings] == ["fixture"]
