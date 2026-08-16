"""Briefing-bound mathematical apparatus for production readiness.

The generic runtime can launch any authenticated cohort.  This module adds the
narrow AMF production contract: every problem in the exact briefing must bind
one content-pinned executable verifier from the materialized core-lock source.
It intentionally performs no model/provider work and no verifier execution
during preflight.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Mapping, NoReturn

from .runtime.auth import PreparedCohort
from .runtime.context import ContextWindowPolicy
from .runtime.contracts import RuntimeBackend
from .runtime.orchestrator import RuntimeLimits
from .runtime.publish import PublicationIdentity
from .source_materializer import SourceMaterializer
from .verifier import (
    AmfVerifierService,
    TargetVerifierBinding,
    VerifierPortfolioIdentity,
    VerificationReceipt,
)
from .world.records import canonical_json


AMF_APPARATUS_PROTOCOL = "PMW_AMF_APPARATUS_1"
MAXIMUM_APPARATUS_BRIEFING_BYTES = 64 * 1024 * 1024
MAXIMUM_APPARATUS_TARGETS = 4_096

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ADMISSION_REF = re.compile(r"^admission/sha256/[0-9a-f]{64}$")
_VERIFIER_RECEIPT_REF = re.compile(r"^verifier-receipt/sha256/([0-9a-f]{64})$")

_SITUATION_FIELDS = {
    "schema",
    "world_id",
    "world_ref",
    "snapshot_ref",
    "problem_count",
    "admission_count",
    "problems",
    "records",
    "unscoped_admission_refs",
    "schema_counts",
    "semantics",
}
_PROBLEM_ROW_FIELDS = {
    "problem_id",
    "target_card_admission_ref",
    "target_card_ref",
    "problem",
    "omitted_historical_runtime_contract",
    "research_admission_refs",
}
_AMF_PROBLEM_FIELDS = {
    "candidate_contract",
    "canonical_statement",
    "claim_scope",
    "formalization_level",
    "hard_gates",
    "partial_success_conditions",
    "portfolio_role",
    "problem_card_sha256",
    "research_context",
    "risks",
    "source_revision",
    "stage",
    "stop_conditions",
    "strict_stage",
    "success_condition",
    "target_card_ref",
    "target_card_sha256",
    "target_id",
    "verification_contract",
    "verification_mode",
}
_VERIFICATION_CONTRACT_FIELDS = {
    "kind",
    "manifest_path",
    "manifest_sha256",
    "registry_path",
    "registry_sha256",
    "source_artifacts",
    "verifier_id",
}
_OMITTED_RUNTIME_FIELDS = {
    "field",
    "sha256",
    "bytes",
    "semantics",
    "exact_content",
}
_SOURCE_ARTIFACT_FIELDS = {"path", "bytes", "sha256"}
_SITUATION_SEMANTICS = {
    "problem_cards": "MATHEMATICAL_CONTENT_WITH_PREDECESSOR_RUNTIME_BUDGET_OMITTED",
    "runtime_authority": "HOST_INVOCATION_AND_LAUNCH_ONLY_NOT_HISTORICAL_WORLD_RECORDS",
    "records": "ONE_BOUNDED_LOSS_AWARE_PROJECTION_PER_NON_CARD_ADMISSION",
    "problem_links": "RESEARCH_ADMISSION_REFS_JOIN_TO_RECORDS",
    "truth_ranking": "NONE",
    "exact_record_access": "world.get(admission_ref, snapshot_ref)",
}


class ApparatusError(ValueError):
    """The authenticated briefing and frozen verifier portfolio disagree."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _fail(code: str, detail: str = "") -> NoReturn:
    raise ApparatusError(code, detail)


def _strict_briefing(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or not 1 <= len(raw) <= MAXIMUM_APPARATUS_BRIEFING_BYTES:
        _fail("AMF_APPARATUS_BRIEFING_SIZE_INVALID")

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                _fail("AMF_APPARATUS_BRIEFING_INVALID", "duplicate key")
            value[key] = item
        return value

    def reject_number(value: str) -> NoReturn:
        _fail("AMF_APPARATUS_BRIEFING_INVALID", f"unsupported number {value[:32]}")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except ApparatusError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ApparatusError("AMF_APPARATUS_BRIEFING_INVALID") from error
    if type(value) is not dict:
        _fail("AMF_APPARATUS_BRIEFING_INVALID", "root")
    if raw != canonical_json(value) + b"\n":
        _fail("AMF_APPARATUS_BRIEFING_NONCANONICAL")
    return value


def _strict_source_json(raw: bytes, *, code: str) -> dict[str, object]:
    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                _fail(code, "duplicate key")
            value[key] = item
        return value

    def reject_number(value: str) -> NoReturn:
        _fail(code, f"unsupported number {value[:32]}")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except ApparatusError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ApparatusError(code) from error
    if type(value) is not dict:
        _fail(code, "root")
    return value


def _source_relative(value: object, *, field: str) -> str:
    selected = _text(value, field=field, maximum_bytes=2_048)
    pure = PurePosixPath(selected)
    if (
        pure.is_absolute()
        or pure.as_posix() != selected
        or any(part in {"", ".", ".."} for part in pure.parts)
        or "\\" in selected
    ):
        _fail("AMF_SOURCE_AUTHORITY_INVALID", field)
    return selected


def _read_source_regular(root: Path, relative: str, *, maximum_bytes: int) -> bytes:
    selected = _source_relative(relative, field="source_path")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        current = os.open(root, flags)
    except OSError as error:
        raise ApparatusError("AMF_SOURCE_AUTHORITY_UNAVAILABLE", selected) from error
    descriptor: int | None = None
    try:
        parts = PurePosixPath(selected).parts
        for part in parts[:-1]:
            next_descriptor = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_descriptor
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(parts[-1], file_flags, dir_fd=current)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= maximum_bytes:
            _fail("AMF_SOURCE_AUTHORITY_INVALID", selected)
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if (
            len(raw) > maximum_bytes
            or len(raw) != before.st_size
            or identity(before) != identity(after)
        ):
            _fail("AMF_SOURCE_AUTHORITY_UNSTABLE", selected)
        return raw
    except ApparatusError:
        raise
    except OSError as error:
        raise ApparatusError("AMF_SOURCE_AUTHORITY_UNAVAILABLE", selected) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(current)


def _text(value: object, *, field: str, maximum_bytes: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum_bytes
    ):
        _fail("AMF_APPARATUS_TARGET_INVALID", field)
    return value


def _digest(value: object, *, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail("AMF_APPARATUS_TARGET_INVALID", field)
    return value


def target_bindings_from_briefing(raw: bytes) -> tuple[TargetVerifierBinding, ...]:
    """Derive every verifier authority from one authenticated briefing.

    No session, target digest, verifier ID or manifest pin is supplied by an
    agent-facing request.  The request may later select only among this frozen
    portfolio.
    """

    value = _strict_briefing(raw)
    if (
        value.get("schema") != "PMW_MATHEMATICAL_SITUATION_2"
        or set(value) != _SITUATION_FIELDS
        or value.get("semantics") != _SITUATION_SEMANTICS
    ):
        _fail("AMF_APPARATUS_BRIEFING_SCHEMA_UNSUPPORTED")
    rows = value.get("problems")
    declared_count = value.get("problem_count")
    if (
        type(rows) is not list
        or not rows
        or len(rows) > MAXIMUM_APPARATUS_TARGETS
        or type(declared_count) is not int
        or declared_count != len(rows)
    ):
        _fail("AMF_APPARATUS_PORTFOLIO_INVALID")

    bindings: list[TargetVerifierBinding] = []
    seen: set[str] = set()
    for ordinal, row in enumerate(rows):
        if (
            type(row) is not dict
            or set(row) != _PROBLEM_ROW_FIELDS
            or type(row.get("problem")) is not dict
        ):
            _fail("AMF_APPARATUS_TARGET_INVALID", str(ordinal))
        problem = row["problem"]
        if set(problem) != _AMF_PROBLEM_FIELDS:
            _fail("AMF_APPARATUS_TARGET_INVALID", f"problem_fields:{ordinal}")
        target_id = _text(problem.get("target_id"), field="target_id", maximum_bytes=128)
        if _ID.fullmatch(target_id) is None or target_id in seen:
            _fail("AMF_APPARATUS_TARGET_INVALID", target_id)
        seen.add(target_id)
        target_sha256 = _digest(
            problem.get("target_card_sha256"), field="target_card_sha256"
        )
        if problem.get("target_card_ref") != f"target-card/sha256/{target_sha256}":
            _fail("AMF_APPARATUS_TARGET_INVALID", f"target_card_ref:{target_id}")
        if problem.get("stage") != "experimental_eligible":
            _fail("AMF_APPARATUS_TARGET_INVALID", f"stage:{target_id}")
        if (
            row.get("problem_id") != target_id
            or row.get("target_card_ref") != problem.get("target_card_ref")
            or type(row.get("target_card_admission_ref")) is not str
            or _ADMISSION_REF.fullmatch(row["target_card_admission_ref"]) is None
        ):
            _fail("AMF_APPARATUS_TARGET_INVALID", f"row_identity:{target_id}")
        research_refs = row.get("research_admission_refs")
        if (
            type(research_refs) is not list
            or len(research_refs) != len(set(research_refs))
            or any(
                type(reference) is not str
                or _ADMISSION_REF.fullmatch(reference) is None
                for reference in research_refs
            )
        ):
            _fail("AMF_APPARATUS_TARGET_INVALID", f"research_refs:{target_id}")
        omitted = row.get("omitted_historical_runtime_contract")
        if omitted is not None:
            if (
                type(omitted) is not dict
                or set(omitted) != _OMITTED_RUNTIME_FIELDS
                or omitted.get("field") != "budget_contract"
                or omitted.get("semantics")
                != "NON_OPERATIVE_PREDECESSOR_CAMPAIGN_PROVENANCE"
                or type(omitted.get("bytes")) is not int
                or omitted["bytes"] < 1
            ):
                _fail(
                    "AMF_APPARATUS_TARGET_INVALID", f"omitted_runtime:{target_id}"
                )
            _digest(omitted.get("sha256"), field="omitted_runtime.sha256")
            if omitted.get("exact_content") != {
                "method": "world.get",
                "admission_ref": row["target_card_admission_ref"],
            }:
                _fail(
                    "AMF_APPARATUS_TARGET_INVALID",
                    f"omitted_runtime_ref:{target_id}",
                )
        mode = _text(
            problem.get("verification_mode"), field="verification_mode"
        )
        contract = problem.get("verification_contract")
        if (
            type(contract) is not dict
            or set(contract) != _VERIFICATION_CONTRACT_FIELDS
            or contract.get("kind") != "EXACT_EXECUTABLE"
        ):
            _fail("AMF_APPARATUS_TARGET_NOT_EXECUTABLE", target_id)
        if contract.get("registry_path") != "data/verifiers.json":
            _fail("AMF_APPARATUS_TARGET_INVALID", f"registry_path:{target_id}")
        source_artifacts = contract.get("source_artifacts")
        if type(source_artifacts) is not list or not source_artifacts:
            _fail("AMF_APPARATUS_TARGET_INVALID", f"source_artifacts:{target_id}")
        source_paths: set[str] = set()
        for artifact in source_artifacts:
            if (
                type(artifact) is not dict
                or set(artifact) != _SOURCE_ARTIFACT_FIELDS
                or type(artifact.get("bytes")) is not int
                or artifact["bytes"] < 1
            ):
                _fail("AMF_APPARATUS_TARGET_INVALID", f"source_artifact:{target_id}")
            artifact_path = _source_relative(
                artifact.get("path"), field="source_artifact.path"
            )
            if artifact_path in source_paths:
                _fail("AMF_APPARATUS_TARGET_INVALID", f"source_artifact:{target_id}")
            source_paths.add(artifact_path)
            _digest(artifact.get("sha256"), field="source_artifact.sha256")
        binding = TargetVerifierBinding(
            target_id=target_id,
            target_sha256=target_sha256,
            verification_mode=mode,
            verifier_id=_text(
                contract.get("verifier_id"), field="verifier_id", maximum_bytes=128
            ),
            registry_sha256=_digest(
                contract.get("registry_sha256"), field="registry_sha256"
            ),
            manifest_path=_text(
                contract.get("manifest_path"), field="manifest_path", maximum_bytes=1_024
            ),
            manifest_sha256=_digest(
                contract.get("manifest_sha256"), field="manifest_sha256"
            ),
        )
        bindings.append(binding)
    return tuple(bindings)


@dataclass(frozen=True, slots=True)
class AmfSourceAuthorityIdentity:
    portfolio_sha256: str
    target_card_closure_sha256: str
    target_count: int


def _audit_amf_source_authority(
    *,
    briefing_bytes: bytes,
    source_materializer: SourceMaterializer,
    bindings: tuple[TargetVerifierBinding, ...],
    registry_sha256: str,
) -> AmfSourceAuthorityIdentity:
    """Cross-check the world briefing against locked AMF target-card bytes."""

    materialized = source_materializer.audit("agent-math-frontier")
    source_root = materialized.tree_path
    portfolio_raw = _read_source_regular(
        source_root, "data/experimental-portfolio.json", maximum_bytes=16 * 1024 * 1024
    )
    portfolio = _strict_source_json(
        portfolio_raw, code="AMF_EXPERIMENTAL_PORTFOLIO_INVALID"
    )
    rows = portfolio.get("targets")
    if (
        portfolio.get("schema") != "AMF_EXPERIMENTAL_PORTFOLIO_1"
        or portfolio.get("verifier_registry_sha256") != registry_sha256
        or type(rows) is not list
        or len(rows) != len(bindings)
    ):
        _fail("AMF_EXPERIMENTAL_PORTFOLIO_INVALID")

    briefing = _strict_briefing(briefing_bytes)
    problem_rows = briefing.get("problems")
    if type(problem_rows) is not list:
        _fail("AMF_APPARATUS_PORTFOLIO_INVALID")
    problems: dict[str, dict[str, object]] = {}
    for row in problem_rows:
        problem = row.get("problem") if type(row) is dict else None
        target_id = problem.get("target_id") if type(problem) is dict else None
        if type(target_id) is not str or target_id in problems:
            _fail("AMF_APPARATUS_PORTFOLIO_INVALID")
        problems[target_id] = problem
    by_target = {item.target_id: item for item in bindings}
    if set(problems) != set(by_target):
        _fail("AMF_SOURCE_TARGET_SET_MISMATCH")

    closure: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        if type(row) is not dict:
            _fail("AMF_EXPERIMENTAL_PORTFOLIO_INVALID")
        target_id = row.get("problem_id")
        if type(target_id) is not str or target_id in seen or target_id not in by_target:
            _fail("AMF_SOURCE_TARGET_SET_MISMATCH", str(target_id))
        seen.add(target_id)
        binding = by_target[target_id]
        problem = problems[target_id]
        target_card_pin = row.get("target_card")
        if type(target_card_pin) is not dict or set(target_card_pin) != {
            "path",
            "bytes",
            "sha256",
        }:
            _fail("AMF_TARGET_CARD_PIN_INVALID", target_id)
        card_path = _source_relative(
            target_card_pin.get("path"), field="target_card.path"
        )
        card_bytes = target_card_pin.get("bytes")
        card_sha256 = _digest(
            target_card_pin.get("sha256"), field="target_card.sha256"
        )
        if (
            type(card_bytes) is not int
            or card_bytes < 1
            or card_sha256 != binding.target_sha256
        ):
            _fail("AMF_TARGET_CARD_PIN_INVALID", target_id)
        card_raw = _read_source_regular(
            source_root, card_path, maximum_bytes=16 * 1024 * 1024
        )
        if (
            len(card_raw) != card_bytes
            or hashlib.sha256(card_raw).hexdigest() != card_sha256
        ):
            _fail("AMF_TARGET_CARD_PIN_MISMATCH", target_id)
        card = _strict_source_json(card_raw, code="AMF_TARGET_CARD_INVALID")
        source_pairs = {
            "problem_id": "target_id",
            "problem_card_sha256": "problem_card_sha256",
            "canonical_statement": "canonical_statement",
            "claim_scope": "claim_scope",
            "source_revision": "source_revision",
            "stop_conditions": "stop_conditions",
            "success_criterion": "success_condition",
            "verifier_id": None,
        }
        if card.get("schema") != "AMF_TARGET_CARD_1":
            _fail("AMF_TARGET_CARD_INVALID", target_id)
        for source_field, briefing_field in source_pairs.items():
            expected = (
                binding.verifier_id
                if briefing_field is None
                else problem.get(briefing_field)
            )
            if card.get(source_field) != expected:
                _fail(
                    "AMF_TARGET_CARD_BRIEFING_MISMATCH",
                    f"{target_id}:{source_field}",
                )
        partial = card.get("partial_progress_criterion")
        if type(partial) is not str or problem.get("partial_success_conditions") != [partial]:
            _fail(
                "AMF_TARGET_CARD_BRIEFING_MISMATCH",
                f"{target_id}:partial_progress_criterion",
            )
        portfolio_pairs = {
            "problem_card_sha256": problem.get("problem_card_sha256"),
            "verification_mode": binding.verification_mode,
            "verifier_id": binding.verifier_id,
            "formalization_level": problem.get("formalization_level"),
            "hard_gates": problem.get("hard_gates"),
            "role": problem.get("portfolio_role"),
            "strict_stage": problem.get("strict_stage"),
        }
        for field, expected in portfolio_pairs.items():
            if row.get(field) != expected:
                _fail("AMF_PORTFOLIO_BRIEFING_MISMATCH", f"{target_id}:{field}")

        candidate_pin = card.get("candidate_schema")
        if type(candidate_pin) is not dict or set(candidate_pin) != {
            "path",
            "bytes",
            "sha256",
        }:
            _fail("AMF_CANDIDATE_SCHEMA_PIN_INVALID", target_id)
        candidate_path = _source_relative(
            candidate_pin.get("path"), field="candidate_schema.path"
        )
        candidate_bytes = candidate_pin.get("bytes")
        candidate_sha256 = _digest(
            candidate_pin.get("sha256"), field="candidate_schema.sha256"
        )
        if type(candidate_bytes) is not int or candidate_bytes < 1:
            _fail("AMF_CANDIDATE_SCHEMA_PIN_INVALID", target_id)
        candidate_raw = _read_source_regular(
            source_root, candidate_path, maximum_bytes=16 * 1024 * 1024
        )
        if (
            len(candidate_raw) != candidate_bytes
            or hashlib.sha256(candidate_raw).hexdigest() != candidate_sha256
        ):
            _fail("AMF_CANDIDATE_SCHEMA_PIN_MISMATCH", target_id)
        candidate_schema = _strict_source_json(
            candidate_raw, code="AMF_CANDIDATE_SCHEMA_INVALID"
        )
        expected_contract = {
            "media_type": "application/schema+json",
            "schema": candidate_schema,
            "source_bytes": candidate_bytes,
            "source_path": candidate_path,
            "source_sha256": candidate_sha256,
        }
        if problem.get("candidate_contract") != expected_contract:
            _fail("AMF_CANDIDATE_SCHEMA_BRIEFING_MISMATCH", target_id)
        verifier_contract = problem.get("verification_contract")
        if type(verifier_contract) is not dict:
            _fail("AMF_TARGET_CARD_BRIEFING_MISMATCH", f"{target_id}:verifier")
        verifier_manifest_raw = _read_source_regular(
            source_root, binding.manifest_path, maximum_bytes=16 * 1024 * 1024
        )
        if hashlib.sha256(verifier_manifest_raw).hexdigest() != binding.manifest_sha256:
            _fail("AMF_VERIFIER_MANIFEST_PIN_MISMATCH", target_id)
        verifier_manifest = _strict_source_json(
            verifier_manifest_raw, code="AMF_VERIFIER_MANIFEST_INVALID"
        )
        if verifier_contract.get("source_artifacts") != verifier_manifest.get(
            "source_artifacts"
        ):
            _fail("AMF_VERIFIER_CONTRACT_BRIEFING_MISMATCH", target_id)
        closure.append(
            {
                "target_id": target_id,
                "target_card": {
                    "path": card_path,
                    "bytes": card_bytes,
                    "sha256": card_sha256,
                },
                "candidate_schema": {
                    "path": candidate_path,
                    "bytes": candidate_bytes,
                    "sha256": candidate_sha256,
                },
            }
        )
    if seen != set(by_target):
        _fail("AMF_SOURCE_TARGET_SET_MISMATCH")
    if source_materializer.audit("agent-math-frontier") != materialized:
        _fail("AMF_SOURCE_AUTHORITY_DRIFT")
    closure.sort(key=lambda item: str(item["target_id"]))
    return AmfSourceAuthorityIdentity(
        portfolio_sha256=hashlib.sha256(canonical_json(portfolio)).hexdigest(),
        target_card_closure_sha256=hashlib.sha256(canonical_json(closure)).hexdigest(),
        target_count=len(closure),
    )


@dataclass(frozen=True, slots=True)
class AmfApparatusPreflightChecker:
    """Required production checker for source, catalog and target bindings."""

    source_materializer: SourceMaterializer
    name: str = "amf-apparatus"

    def verify(
        self,
        *,
        prepared: PreparedCohort,
        backend: RuntimeBackend,
        limits: RuntimeLimits,
        context_policy: ContextWindowPolicy,
        publication_identity: PublicationIdentity,
    ) -> Mapping[str, object]:
        # The unused generic launch values are deliberately accepted so this
        # remains a normal PreflightChecker.  Their own checks are authoritative.
        del backend, limits, context_policy, publication_identity
        bindings = target_bindings_from_briefing(prepared.briefing_bytes)
        identity = AmfVerifierService.audit_portfolio(
            source_materializer=self.source_materializer,
            target_bindings=bindings,
        )
        authority = _audit_amf_source_authority(
            briefing_bytes=prepared.briefing_bytes,
            source_materializer=self.source_materializer,
            bindings=bindings,
            registry_sha256=identity.registry_sha256,
        )
        source_tree = self.source_materializer.audit(
            "agent-math-frontier"
        ).tree_path
        return {
            "protocol": AMF_APPARATUS_PROTOCOL,
            "briefing_sha256": prepared.plan.briefing_sha256,
            "source_commit": identity.commit,
            "source_tree_sha256": identity.materializer_tree_sha256,
            "source_tree_path": str(source_tree),
            "registry_sha256": identity.registry_sha256,
            "catalog_verifier_count": identity.catalog_verifier_count,
            "target_count": identity.target_count,
            "target_bindings_sha256": identity.target_bindings_sha256,
            "source_portfolio_sha256": authority.portfolio_sha256,
            "target_card_closure_sha256": authority.target_card_closure_sha256,
            "verifier_execution": False,
            "model_or_network_calls": 0,
        }


@dataclass(frozen=True, slots=True)
class ReadinessScopeChecker:
    """Make a runtime-only versus math-production PASS impossible to confuse."""

    scope: str
    name: str = "readiness-scope"

    def __post_init__(self) -> None:
        if self.scope not in {"runtime-only", "amf-production"}:
            raise ValueError("unsupported readiness scope")

    def verify(
        self,
        *,
        prepared: PreparedCohort,
        backend: RuntimeBackend,
        limits: RuntimeLimits,
        context_policy: ContextWindowPolicy,
        publication_identity: PublicationIdentity,
    ) -> Mapping[str, object]:
        del prepared, backend, limits, context_policy, publication_identity
        return {
            "scope": self.scope,
            "mathematical_apparatus_required": self.scope == "amf-production",
            "meaning": (
                "RUNTIME_AND_BRIEFING_BOUND_AMF_APPARATUS_READY"
                if self.scope == "amf-production"
                else "GENERIC_RUNTIME_READY_MATH_APPARATUS_NOT_ASSERTED"
            ),
        }


def audit_amf_apparatus(
    prepared: PreparedCohort,
    source_materializer: SourceMaterializer,
) -> VerifierPortfolioIdentity:
    """Re-audit the complete briefing/source/verifier authority closure.

    This check is required even when the cohort originally launched with the
    narrower ``runtime-only`` readiness scope.  A later host-verifier receipt
    must never promote a merely verifier-shaped, source-inconsistent briefing.
    """

    bindings = target_bindings_from_briefing(prepared.briefing_bytes)
    identity = AmfVerifierService.audit_portfolio(
        source_materializer=source_materializer,
        target_bindings=bindings,
    )
    _audit_amf_source_authority(
        briefing_bytes=prepared.briefing_bytes,
        source_materializer=source_materializer,
        bindings=bindings,
        registry_sha256=identity.registry_sha256,
    )
    return identity


def persist_verification_receipt(
    evidence_root: Path,
    receipt: VerificationReceipt,
) -> Path:
    """Durably publish one immutable host-verifier receipt after settlement."""

    if not isinstance(evidence_root, Path) or not isinstance(
        receipt, VerificationReceipt
    ):
        raise TypeError("evidence_root and receipt must be typed")
    try:
        parent_metadata = evidence_root.lstat()
        if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(
            parent_metadata.st_mode
        ):
            _fail("VERIFIER_LEDGER_PATH_UNSAFE")
        ledger = evidence_root / "verifier-receipts"
        if ledger.exists() or ledger.is_symlink():
            metadata = ledger.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                _fail("VERIFIER_LEDGER_PATH_UNSAFE")
        else:
            ledger.mkdir(mode=0o700)
        ledger = ledger.resolve(strict=True)
        if ledger.parent != evidence_root.resolve(strict=True):
            _fail("VERIFIER_LEDGER_PATH_UNSAFE")
    except ApparatusError:
        raise
    except OSError as error:
        raise ApparatusError("VERIFIER_LEDGER_PATH_UNAVAILABLE") from error

    value = receipt.as_dict()
    match = _VERIFIER_RECEIPT_REF.fullmatch(receipt.receipt_ref)
    raw = canonical_json(value) + b"\n"
    if match is None or not 1 <= len(raw) <= 2 * 1024 * 1024:
        _fail("VERIFIER_RECEIPT_IDENTITY_INVALID")
    core = dict(value)
    if core.pop("receipt_ref", None) != receipt.receipt_ref or hashlib.sha256(
        canonical_json(core)
    ).hexdigest() != match.group(1):
        _fail("VERIFIER_RECEIPT_IDENTITY_INVALID")
    destination = ledger / f"{match.group(1)}.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".receipt.", dir=ledger)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("VERIFIER_RECEIPT_WRITE_FAILED")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary_name, destination, follow_symlinks=False)
        except FileExistsError:
            existing = _read_source_regular(
                ledger, destination.name, maximum_bytes=2 * 1024 * 1024
            )
            if existing != raw:
                _fail("VERIFIER_RECEIPT_CONFLICT")
        directory_descriptor = os.open(ledger, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except ApparatusError:
        raise
    except OSError as error:
        raise ApparatusError("VERIFIER_RECEIPT_WRITE_FAILED") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return destination


__all__ = [
    "AMF_APPARATUS_PROTOCOL",
    "AmfApparatusPreflightChecker",
    "ApparatusError",
    "ReadinessScopeChecker",
    "audit_amf_apparatus",
    "persist_verification_receipt",
    "target_bindings_from_briefing",
]
