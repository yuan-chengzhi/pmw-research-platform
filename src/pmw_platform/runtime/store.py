"""Durable, backend-neutral storage for one runtime launch.

The store deliberately knows nothing about model providers or PMW publishing.
It owns only the small trusted boundary around a launch: an exclusive cohort
claim, a symlink-free private layout, canonical JSON documents, and immutable
terminal receipts.

Callers must hold :class:`RuntimeClaim` for the whole launch lifetime.  The
filesystem operations remain exclusive on their own so that an omitted claim
fails closed instead of overwriting an existing run.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from dataclasses import dataclass
from typing import Mapping, Sequence

from ..world.records import canonical_json
from .contracts import (
    BackendIdentity,
    MAXIMUM_STOP_GRACE_SECONDS,
    RuntimeContractError,
    runtime_host_policy_value,
)
from .context import (
    CONTEXT_WINDOW_SEMANTICS,
    ContextWindowControl,
    ContextWindowPolicy,
)
from .usage import usage_evidence_value_is_valid


RUNTIME_LAUNCH_SCHEMA = "PMW_RUNTIME_LAUNCH_1"
RUNTIME_STATE_SCHEMA = "PMW_RUNTIME_SESSION_STATE_1"
RUNTIME_RECEIPT_SCHEMA = "PMW_RUNTIME_SESSION_RECEIPT_1"
RUNTIME_SETTLEMENT_SCHEMA = "PMW_RUNTIME_SETTLEMENT_1"
MAXIMUM_RUNTIME_DOCUMENT_BYTES = 16 * 1024 * 1024
MAXIMUM_STATE_BYTES = 1 * 1024 * 1024
MAXIMUM_INPUT_BYTES = 64 * 1024 * 1024
MAXIMUM_SESSIONS = 4_096
MAXIMUM_VERIFIER_KIT_LAUNCH_BYTES = 8_192

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"})
_SNAPSHOT_REF = re.compile(r"^snapshot/sha256/[0-9a-f]{64}$")
_LAUNCH_FIELDS = frozenset({
    "schema",
    "created_at",
    "cohort_id",
    "world_id",
    "world_ref",
    "base_snapshot_ref",
    "plan_sha256",
    "briefing_sha256",
    "safety_profile",
    "safety_profile_sha256",
    "core_lock_sha256",
    "backend",
    "backend_sha256",
    "publication",
    "publication_sha256",
    "concurrency",
    "session_ids",
    "limits",
    "context_window_policy",
    "backend_context_window_control",
    "required_readiness",
    "required_readiness_sha256",
    "verifier_kit",
    "verifier_kit_sha256",
    "agenda_arm",
    "agenda_arm_sha256",
    "host_policy",
})
# Literal in-session verifier-kit vocabulary, kept here for the same reason the
# publication and resource-guard vocabularies are: the durable store validates
# its own documents and must not import a producer to do it.
_VERIFIER_KIT_LAUNCH_SCHEMA = "PMW_IN_SESSION_VERIFIER_KIT_LAUNCH_1"
_VERIFIER_KIT_EVIDENCE_SCHEMA = "PMW_IN_SESSION_VERIFIER_KIT_SESSION_EVIDENCE_1"
_IN_SESSION_VERIFIER_AUTHORITY = "ADVISORY_IN_SESSION_VERIFICATION"
_SETTLEMENT_VERIFIER_AUTHORITY = "HOST_REEXECUTED_PINNED_AMF_VERIFIER"
_VERIFIER_KIT_MODES = frozenset({"MATERIALIZED", "DISABLED"})
_VERIFIER_KIT_LEDGERS = frozenset(
    {"OBSERVED", "ABSENT", "UNREADABLE", "NOT_MATERIALIZED"}
)
_VERIFIER_VERDICT_STATUSES = frozenset({"PASS", "REJECTED", "APPARATUS_ERROR"})
_VERIFIER_KIT_EVIDENCE_FIELDS = frozenset({
    "schema",
    "mode",
    "authority",
    "settlement_authority",
    "counting_authority",
    "evidence_directory",
    "kit_content_sha256",
    "ledger",
    "invocation_count",
    "verdict_counts",
    "rejected_entries",
    "truncated",
})
# Agenda-arm vocabulary, held here for the same reason as the verifier-kit
# vocabulary above: the durable store validates its own documents and must not
# import a producer -- least of all an experiment plugin -- to do it.
_AGENDA_ARM_LAUNCH_SCHEMA = "PMW_AGENDA_ARM_LAUNCH_1"
_AGENDA_ARM_EVIDENCE_SCHEMA = "PMW_AGENDA_ARM_SESSION_EVIDENCE_1"
_AGENDA_ARM_MODES = frozenset({"ENFORCED", "NOT_CONFIGURED"})
_AGENDA_ARM_LAUNCH_FIELDS = frozenset({
    "schema",
    "mode",
    "arm",
    "instruments",
    "admitted_payload_schemas",
    "coordinator_session_ids",
    "admitting_slots",
    "open_admission",
    "require_claim_for_primary_action",
    "enforce_directive_citation",
    "agenda_clock",
    "lease_release",
    "enforcement",
    "rejection_semantics",
})
_AGENDA_ARM_LAUNCH_ABSENT_FIELDS = frozenset({
    "schema",
    "mode",
    "reason",
    "enforcement",
})
_AGENDA_ARM_EVIDENCE_FIELDS = frozenset({
    "schema",
    "mode",
    "arm",
    "arm_sha256",
    "reviewed",
    "admitted",
    "rejected",
    "verdicts",
    "instrument_attempts",
    "records_by_schema",
    "route_declarations",
    "lease_release",
    "agenda_clock",
    "decisions",
    "truncated",
    "publication_divergences",
    "rejection_semantics",
})
_AGENDA_ARM_EVIDENCE_ABSENT_FIELDS = frozenset({
    "schema",
    "mode",
    "arm",
    "arm_sha256",
    "reviewed",
    "admitted",
    "rejected",
    "reason",
})
_AGENDA_ARM_ROUTE_FIELDS = frozenset({
    "count",
    "with_peer_trigger_refs",
    "resolved_peer_trigger_refs",
    "dangling_rejected",
    "differentiation_notes",
})
_AGENDA_ARM_DECISION_FIELDS = frozenset({
    "ordinal",
    "kind",
    "payload_schema",
    "instrument",
    "code",
    "admitted",
    "detail",
})
_AGENDA_VERDICT_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAXIMUM_AGENDA_ARM_LAUNCH_BYTES = 262_144
MAXIMUM_AGENDA_ARM_EVIDENCE_BYTES = 262_144
MAXIMUM_AGENDA_ARM_DECISIONS = 64
_RECEIPT_FIELDS = frozenset({
    "schema",
    "launch_sha256",
    "plan_sha256",
    "cohort_id",
    "session_id",
    "world_id",
    "world_ref",
    "base_snapshot_ref",
    "backend_sha256",
    "status",
    "terminal_reason",
    "started_at",
    "finished_at",
    "stop_proof",
    "outcome",
    "publications",
    "error",
    "resource_guard",
    "usage",
    "verifier_kit",
    "agenda_arm",
    "context_window",
})
_STOP_PROOF_FIELDS = frozenset({
    "stopped",
    "reason",
    "forced",
    "process_group_id",
    "detail",
})
_OUTCOME_FIELDS = frozenset({
    "success",
    "terminal_reason",
    "summary",
    "usage",
    "evidence",
    "contribution_count",
    "contribution_sha256",
})
_ERROR_FIELDS = frozenset({"code", "type"})
_RESOURCE_GUARD_FIELDS = frozenset({
    "schema",
    "checks",
    "latest",
    "terminal_event",
    "warnings",
})
_RESOURCE_CHECK_FIELDS = frozenset({"disk", "workspace", "cache"})
_RESOURCE_TREE_SNAPSHOT_FIELDS = frozenset({
    "total_bytes",
    "entries",
    "maximum_depth",
})
_RESOURCE_DISK_SNAPSHOT_FIELDS = frozenset({
    "total_bytes",
    "available_bytes",
    "required_free_bytes",
})
_RESOURCE_EVENT_FIELDS = frozenset({
    "code",
    "scope",
    "target",
    "phase",
    "disposition",
    "session_id",
    "uncertain",
    "observed",
    "limits",
    "detail",
})
_RECEIPT_CONTEXT_WINDOW_FIELDS = frozenset({
    "semantics",
    "configured_tokens",
    "backend_control",
    "strict_pre_http_input_gate",
})
_SETTLEMENT_FIELDS = frozenset({
    "schema",
    "launch_sha256",
    "plan_sha256",
    "cohort_id",
    "finished_at",
    "outcome",
    "counts",
    "receipts",
})
_SETTLEMENT_RECEIPT_FIELDS = frozenset({
    "session_id",
    "status",
    "receipt_sha256",
})


class RuntimeStoreError(ValueError):
    """A runtime path or durable document is unsafe, conflicting, or invalid."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _fail(code: str, detail: str = "") -> None:
    raise RuntimeStoreError(code, detail)


def _identifier(value: object, *, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail("MALFORMED_RUNTIME_IDENTIFIER", label)
    return value


def _bounded_utc_timestamp(value: object) -> bool:
    return (
        type(value) is str
        and value.endswith("Z")
        and 1 < len(value.encode("utf-8")) <= 64
    )


def _bounded_json_object(value: object, *, maximum_bytes: int) -> bool:
    if type(value) is not dict:
        return False
    try:
        return len(canonical_json(value)) <= maximum_bytes
    except Exception:
        return False


def _validate_stop_proof(value: object, *, session_id: str) -> bool | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != _STOP_PROOF_FIELDS:
        _fail("MALFORMED_SESSION_RECEIPT", f"stop_proof {session_id}")
    stopped = value.get("stopped")
    forced = value.get("forced")
    reason = value.get("reason")
    process_group_id = value.get("process_group_id")
    detail = value.get("detail")
    if (
        type(stopped) is not bool
        or type(forced) is not bool
        or type(reason) is not str
        or _TERMINAL_REASON.fullmatch(reason) is None
        or (
            process_group_id is not None
            and (type(process_group_id) is not int or process_group_id <= 0)
        )
        or type(detail) is not str
        or len(detail.encode("utf-8", errors="strict")) > 2_048
    ):
        _fail("MALFORMED_SESSION_RECEIPT", f"stop_proof {session_id}")
    return stopped


def _validate_receipt_outcome(value: object, *, session_id: str) -> bool | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != _OUTCOME_FIELDS:
        _fail("MALFORMED_SESSION_RECEIPT", f"outcome {session_id}")
    success = value.get("success")
    terminal_reason = value.get("terminal_reason")
    summary = value.get("summary")
    usage = value.get("usage")
    evidence = value.get("evidence")
    contribution_count = value.get("contribution_count")
    contribution_sha256 = value.get("contribution_sha256")
    if (
        type(success) is not bool
        or type(terminal_reason) is not str
        or _TERMINAL_REASON.fullmatch(terminal_reason) is None
        or type(summary) is not str
        or len(summary.encode("utf-8", errors="strict")) > 65_536
        or not _bounded_json_object(usage, maximum_bytes=1_048_576)
        or not _bounded_json_object(evidence, maximum_bytes=1_048_576)
        or type(contribution_count) is not int
        or not 0 <= contribution_count <= 64
        or type(contribution_sha256) is not list
        or len(contribution_sha256) != contribution_count
        or any(
            type(digest) is not str or _SHA256.fullmatch(digest) is None
            for digest in contribution_sha256
        )
    ):
        _fail("MALFORMED_SESSION_RECEIPT", f"outcome {session_id}")
    return success


def _validate_receipt_error(value: object, *, session_id: str) -> None:
    if value is None:
        return
    if type(value) is not dict or set(value) != _ERROR_FIELDS:
        _fail("MALFORMED_SESSION_RECEIPT", f"error {session_id}")
    code = value.get("code")
    error_type = value.get("type")
    if (
        type(code) is not str
        or not code
        or len(code.encode("utf-8", errors="strict")) > 128
        or (
            error_type is not None
            and (
                type(error_type) is not str
                or len(error_type.encode("utf-8", errors="strict")) > 512
            )
        )
    ):
        _fail("MALFORMED_SESSION_RECEIPT", f"error {session_id}")


def _resource_nonnegative_object(
    value: object,
    fields: frozenset[str],
) -> bool:
    return (
        type(value) is dict
        and set(value) == fields
        and all(type(value[field]) is int and value[field] >= 0 for field in fields)
    )


def _validate_resource_event(value: object, *, session_id: str) -> None:
    if type(value) is not dict or set(value) != _RESOURCE_EVENT_FIELDS:
        _fail("MALFORMED_SESSION_RECEIPT", f"resource event {session_id}")
    code = value.get("code")
    detail = value.get("detail")
    observed = value.get("observed")
    limits = value.get("limits")
    event_session = value.get("session_id")
    scope = value.get("scope")
    disposition = value.get("disposition")
    uncertain = value.get("uncertain")
    if (
        type(code) is not str
        or _TERMINAL_REASON.fullmatch(code) is None
        or scope not in {"SESSION", "COHORT"}
        or value.get("target") not in {"host_filesystem", "workspace", "cache"}
        or value.get("phase") not in {"INITIAL", "LIVE", "TERMINAL"}
        or disposition not in {None, "SESSION_STOP", "JOB_STOP", "REJECT", "WARN"}
        or (
            event_session is not None
            and (type(event_session) is not str or _IDENTIFIER.fullmatch(event_session) is None)
        )
        or type(uncertain) is not bool
        or (scope == "SESSION" and event_session != session_id)
        or (uncertain and disposition is not None)
        or (not uncertain and disposition is None)
        or type(observed) is not dict
        or len(observed) > 4
        or any(type(key) is not str or type(item) is not int or item < 0 for key, item in observed.items())
        or type(limits) is not dict
        or len(limits) > 4
        or any(type(key) is not str or type(item) is not int or item < 0 for key, item in limits.items())
        or type(detail) is not str
        or len(detail.encode("utf-8", errors="strict")) > 512
    ):
        _fail("MALFORMED_SESSION_RECEIPT", f"resource event {session_id}")


def _validate_resource_guard(value: object, *, session_id: str) -> None:
    if type(value) is not dict or set(value) != _RESOURCE_GUARD_FIELDS:
        _fail("MALFORMED_SESSION_RECEIPT", f"resource_guard {session_id}")
    checks = value.get("checks")
    latest = value.get("latest")
    terminal = value.get("terminal_event")
    warnings = value.get("warnings")
    if (
        value.get("schema") != "PMW_RUNTIME_RESOURCE_EVIDENCE_1"
        or not _resource_nonnegative_object(checks, _RESOURCE_CHECK_FIELDS)
        or type(latest) is not dict
        or set(latest) != _RESOURCE_CHECK_FIELDS
        or (
            latest.get("disk") is not None
            and not _resource_nonnegative_object(
                latest.get("disk"), _RESOURCE_DISK_SNAPSHOT_FIELDS
            )
        )
        or (
            latest.get("workspace") is not None
            and not _resource_nonnegative_object(
                latest.get("workspace"), _RESOURCE_TREE_SNAPSHOT_FIELDS
            )
        )
        or (
            latest.get("cache") is not None
            and not _resource_nonnegative_object(
                latest.get("cache"), _RESOURCE_TREE_SNAPSHOT_FIELDS
            )
        )
        or type(warnings) is not list
        or len(warnings) > 4
    ):
        _fail("MALFORMED_SESSION_RECEIPT", f"resource_guard {session_id}")
    if terminal is not None:
        _validate_resource_event(terminal, session_id=session_id)
        if terminal.get("disposition") == "WARN":
            _fail("MALFORMED_SESSION_RECEIPT", f"resource_guard {session_id}")
    for warning in warnings:
        _validate_resource_event(warning, session_id=session_id)
        if warning.get("disposition") != "WARN":
            _fail("MALFORMED_SESSION_RECEIPT", f"resource_guard {session_id}")


def _validate_launch_verifier_kit(launch: Mapping[str, object]) -> None:
    """Validate the launch-bound identity of the in-session verifier kit."""

    value = launch.get("verifier_kit")
    if type(value) is not dict:
        _fail("MALFORMED_RUNTIME_LAUNCH", "verifier_kit")
    raw = canonical_json(value)
    if (
        value.get("schema") != _VERIFIER_KIT_LAUNCH_SCHEMA
        or value.get("mode") not in _VERIFIER_KIT_MODES
        or value.get("authority") != _IN_SESSION_VERIFIER_AUTHORITY
        or value.get("settlement_authority") != _SETTLEMENT_VERIFIER_AUTHORITY
        or len(raw) > MAXIMUM_VERIFIER_KIT_LAUNCH_BYTES
        or hashlib.sha256(raw).hexdigest() != launch.get("verifier_kit_sha256")
    ):
        _fail("MALFORMED_RUNTIME_LAUNCH", "verifier_kit")
    if value["mode"] == "MATERIALIZED":
        for label in (
            "kit_sha256",
            "content_sha256",
            "manifest_sha256",
            "interpreter_sha256",
            "target_ids_sha256",
        ):
            digest = value.get(label)
            if type(digest) is not str or _SHA256.fullmatch(digest) is None:
                _fail("MALFORMED_RUNTIME_LAUNCH", f"verifier_kit.{label}")
        if (
            # A kit is a workspace input, so its launch identity must state
            # that it carries no credential material.
            value.get("credential_material") is not False
            or type(value.get("file_count")) is not int
            or value["file_count"] < 1  # type: ignore[operator]
            or type(value.get("total_bytes")) is not int
            or value["total_bytes"] < 1  # type: ignore[operator]
            or type(value.get("target_count")) is not int
            or value["target_count"] < 1  # type: ignore[operator]
            or type(value.get("entrypoint")) is not str
            or not value["entrypoint"]
        ):
            _fail("MALFORMED_RUNTIME_LAUNCH", "verifier_kit identity")


def _validate_verifier_kit_evidence(
    value: object,
    *,
    session_id: str,
    launch: Mapping[str, object],
) -> None:
    if type(value) is not dict or set(value) != _VERIFIER_KIT_EVIDENCE_FIELDS:
        _fail("MALFORMED_SESSION_RECEIPT", f"verifier_kit {session_id}")
    counts = value.get("verdict_counts")
    kit_digest = value.get("kit_content_sha256")
    invocation_count = value.get("invocation_count")
    rejected_entries = value.get("rejected_entries")
    if (
        value.get("schema") != _VERIFIER_KIT_EVIDENCE_SCHEMA
        or value.get("mode") not in _VERIFIER_KIT_MODES
        or value.get("authority") != _IN_SESSION_VERIFIER_AUTHORITY
        or value.get("settlement_authority") != _SETTLEMENT_VERIFIER_AUTHORITY
        or type(value.get("counting_authority")) is not str
        or not value["counting_authority"]
        or type(value.get("evidence_directory")) is not str
        or not value["evidence_directory"]
        or value.get("ledger") not in _VERIFIER_KIT_LEDGERS
        or type(value.get("truncated")) is not bool
        or type(invocation_count) is not int
        or invocation_count < 0
        or type(rejected_entries) is not int
        or rejected_entries < 0
        or type(counts) is not dict
        or set(counts) != _VERIFIER_VERDICT_STATUSES
        or any(type(item) is not int or item < 0 for item in counts.values())
        or sum(counts.values()) != invocation_count
        or (
            kit_digest is not None
            and (
                type(kit_digest) is not str
                or _SHA256.fullmatch(kit_digest) is None
            )
        )
    ):
        _fail("MALFORMED_SESSION_RECEIPT", f"verifier_kit {session_id}")
    kit = launch.get("verifier_kit")
    launch_mode = kit.get("mode") if type(kit) is dict else None
    launch_digest = kit.get("content_sha256") if type(kit) is dict else None
    if value["mode"] != launch_mode:
        _fail("MALFORMED_SESSION_RECEIPT", f"verifier_kit mode {session_id}")
    if value["mode"] == "MATERIALIZED":
        if kit_digest != launch_digest or value["ledger"] == "NOT_MATERIALIZED":
            _fail("MALFORMED_SESSION_RECEIPT", f"verifier_kit binding {session_id}")
    elif kit_digest is not None or value["ledger"] != "NOT_MATERIALIZED":
        _fail("MALFORMED_SESSION_RECEIPT", f"verifier_kit binding {session_id}")


def _validate_launch_agenda_arm(launch: Mapping[str, object]) -> None:
    """Validate the launch-bound identity of this cohort's agenda arm."""

    value = launch.get("agenda_arm")
    if type(value) is not dict:
        _fail("MALFORMED_RUNTIME_LAUNCH", "agenda_arm")
    raw = canonical_json(value)
    if (
        value.get("schema") != _AGENDA_ARM_LAUNCH_SCHEMA
        or value.get("mode") not in _AGENDA_ARM_MODES
        or len(raw) > MAXIMUM_AGENDA_ARM_LAUNCH_BYTES
        or hashlib.sha256(raw).hexdigest() != launch.get("agenda_arm_sha256")
    ):
        _fail("MALFORMED_RUNTIME_LAUNCH", "agenda_arm")
    if value["mode"] == "NOT_CONFIGURED":
        if set(value) != _AGENDA_ARM_LAUNCH_ABSENT_FIELDS:
            _fail("MALFORMED_RUNTIME_LAUNCH", "agenda_arm")
        return
    if set(value) != _AGENDA_ARM_LAUNCH_FIELDS:
        _fail("MALFORMED_RUNTIME_LAUNCH", "agenda_arm")
    session_ids = set(RuntimeStore._launch_session_ids(launch))
    instruments = value.get("instruments")
    schemas = value.get("admitted_payload_schemas")
    coordinators = value.get("coordinator_session_ids")
    admitting = value.get("admitting_slots")
    if (
        type(value.get("arm")) is not str
        or not value["arm"]
        or type(instruments) is not list
        or not instruments
        or not all(type(item) is str and item for item in instruments)
        or sorted(set(instruments)) != sorted(instruments)
        or type(schemas) is not list
        or sorted(set(schemas)) != schemas
        or not all(type(item) is str and item for item in schemas)
        or type(coordinators) is not list
        or sorted(set(coordinators)) != coordinators
        or not set(coordinators) <= session_ids
        or type(value.get("open_admission")) is not bool
        or type(value.get("require_claim_for_primary_action")) is not bool
        or type(value.get("enforce_directive_citation")) is not bool
        or type(value.get("agenda_clock")) is not str
        or type(value.get("lease_release")) is not str
        or type(value.get("enforcement")) is not str
        or type(value.get("rejection_semantics")) is not str
    ):
        _fail("MALFORMED_RUNTIME_LAUNCH", "agenda_arm identity")
    # Open admission is the D arm's "any session, at any time"; the explicit
    # form must name only sessions this launch actually runs.
    if value["open_admission"]:
        if admitting != "ALL_SESSIONS":
            _fail("MALFORMED_RUNTIME_LAUNCH", "agenda_arm admitting_slots")
    elif (
        type(admitting) is not list
        or sorted(set(admitting)) != admitting
        or not set(admitting) <= session_ids
    ):
        _fail("MALFORMED_RUNTIME_LAUNCH", "agenda_arm admitting_slots")


def _agenda_counts(value: object, *, expected: frozenset[str] | None) -> int | None:
    """Return the total of a bounded ``str -> non-negative int`` count map."""

    if type(value) is not dict:
        return None
    if expected is not None and set(value) != expected:
        return None
    total = 0
    for key, count in value.items():
        if (
            type(key) is not str
            or not key
            or len(key.encode("utf-8")) > 128
            or type(count) is not int
            or isinstance(count, bool)
            or count < 0
        ):
            return None
        total += count
    return total


def _validate_agenda_arm_evidence(
    value: object,
    *,
    session_id: str,
    launch: Mapping[str, object],
) -> int:
    """Validate one session's agenda-arm settlement evidence.

    Returns the number of contributions the arm rejected, which the receipt's
    contribution accounting needs: every contribution of a successful session is
    either published or rejected with a recorded verdict, and a rejection is a
    research event rather than a failure.
    """

    if type(value) is not dict:
        _fail("MALFORMED_SESSION_RECEIPT", f"agenda_arm {session_id}")
    arm = launch.get("agenda_arm")
    launch_mode = arm.get("mode") if type(arm) is dict else None
    reviewed = value.get("reviewed")
    admitted = value.get("admitted")
    rejected = value.get("rejected")
    if (
        value.get("schema") != _AGENDA_ARM_EVIDENCE_SCHEMA
        or value.get("mode") not in _AGENDA_ARM_MODES
        or value["mode"] != launch_mode
        or type(reviewed) is not int
        or isinstance(reviewed, bool)
        or reviewed < 0
        or type(admitted) is not int
        or isinstance(admitted, bool)
        or admitted < 0
        or type(rejected) is not int
        or isinstance(rejected, bool)
        or rejected < 0
        or admitted + rejected != reviewed
        or len(canonical_json(value)) > MAXIMUM_AGENDA_ARM_EVIDENCE_BYTES
    ):
        _fail("MALFORMED_SESSION_RECEIPT", f"agenda_arm {session_id}")
    if value["mode"] == "NOT_CONFIGURED":
        if (
            set(value) != _AGENDA_ARM_EVIDENCE_ABSENT_FIELDS
            or value.get("arm") is not None
            or value.get("arm_sha256") is not None
            or reviewed != 0
            or type(value.get("reason")) is not str
        ):
            _fail("MALFORMED_SESSION_RECEIPT", f"agenda_arm {session_id}")
        return 0
    if (
        set(value) != _AGENDA_ARM_EVIDENCE_FIELDS
        or value.get("arm") != (arm.get("arm") if type(arm) is dict else None)
        or value.get("arm_sha256") != launch.get("agenda_arm_sha256")
        or type(value.get("truncated")) is not bool
        or type(value.get("publication_divergences")) is not int
        or value["publication_divergences"] < 0  # type: ignore[operator]
        or value.get("rejection_semantics")
        != (arm.get("rejection_semantics") if type(arm) is dict else None)
    ):
        _fail("MALFORMED_SESSION_RECEIPT", f"agenda_arm binding {session_id}")
    verdict_total = _agenda_counts(value.get("verdicts"), expected=None)
    if verdict_total != reviewed:
        _fail("MALFORMED_SESSION_RECEIPT", f"agenda_arm verdicts {session_id}")
    for code in value["verdicts"]:  # type: ignore[index]
        if _AGENDA_VERDICT_CODE.fullmatch(code) is None:
            _fail("MALFORMED_SESSION_RECEIPT", f"agenda_arm verdicts {session_id}")
    if _agenda_counts(value.get("instrument_attempts"), expected=None) is None:
        _fail("MALFORMED_SESSION_RECEIPT", f"agenda_arm instruments {session_id}")
    if _agenda_counts(value.get("records_by_schema"), expected=None) is None:
        _fail("MALFORMED_SESSION_RECEIPT", f"agenda_arm records {session_id}")
    if (
        _agenda_counts(
            value.get("route_declarations"), expected=_AGENDA_ARM_ROUTE_FIELDS
        )
        is None
    ):
        _fail("MALFORMED_SESSION_RECEIPT", f"agenda_arm route {session_id}")
    _validate_agenda_lease_release(value.get("lease_release"), session_id=session_id)
    _validate_agenda_clock(value.get("agenda_clock"), session_id=session_id)
    decisions = value.get("decisions")
    if (
        type(decisions) is not list
        or len(decisions) > MAXIMUM_AGENDA_ARM_DECISIONS
        or (len(decisions) < reviewed and value["truncated"] is not True)
        or len(decisions) > reviewed
    ):
        _fail("MALFORMED_SESSION_RECEIPT", f"agenda_arm decisions {session_id}")
    for ordinal, row in enumerate(decisions, start=1):
        if (
            type(row) is not dict
            or set(row) != _AGENDA_ARM_DECISION_FIELDS
            or row.get("ordinal") != ordinal
            or type(row.get("kind")) is not str
            or type(row.get("code")) is not str
            or _AGENDA_VERDICT_CODE.fullmatch(row["code"]) is None  # type: ignore[arg-type]
            or type(row.get("admitted")) is not bool
            or row["admitted"] is not (row["code"] == "ACCEPTED")
            or type(row.get("detail")) is not str
            or (
                row.get("payload_schema") is not None
                and type(row["payload_schema"]) is not str
            )
            or (
                row.get("instrument") is not None
                and type(row["instrument"]) is not str
            )
        ):
            _fail(
                "MALFORMED_SESSION_RECEIPT", f"agenda_arm decisions {session_id}"
            )
    return rejected


def _validate_agenda_lease_release(value: object, *, session_id: str) -> None:
    references = value.get("released_claim_refs") if type(value) is dict else None
    released_at = value.get("released_at_tick") if type(value) is dict else None
    if (
        type(value) is not dict
        or set(value) != {"authority", "released_claim_refs", "released_at_tick"}
        or type(value.get("authority")) is not str
        or not value["authority"]
        or type(references) is not list
        or len(references) > MAXIMUM_AGENDA_ARM_DECISIONS
        or sorted(set(references)) != references
        or not all(type(item) is str and item for item in references)
        or (
            released_at is not None
            and (
                type(released_at) is not int
                or isinstance(released_at, bool)
                or released_at < 0
            )
        )
        or (references and released_at is None)
    ):
        _fail("MALFORMED_SESSION_RECEIPT", f"agenda_arm lease_release {session_id}")


def _validate_agenda_clock(value: object, *, session_id: str) -> None:
    if type(value) is not dict or set(value) != {
        "semantics",
        "base_tick",
        "settled_tick",
    }:
        _fail("MALFORMED_SESSION_RECEIPT", f"agenda_arm clock {session_id}")
    base = value.get("base_tick")
    settled = value.get("settled_tick")
    if (
        type(value.get("semantics")) is not str
        or not value["semantics"]
        or type(base) is not int
        or isinstance(base, bool)
        or base < 0
        or type(settled) is not int
        or isinstance(settled, bool)
        # The world's admission counter never runs backwards.
        or settled < base
    ):
        _fail("MALFORMED_SESSION_RECEIPT", f"agenda_arm clock {session_id}")


def _canonical_document(value: object, *, maximum_bytes: int) -> bytes:
    if type(value) is not dict:
        _fail("MALFORMED_RUNTIME_DOCUMENT", "root must be an object")
    try:
        raw = canonical_json(value) + b"\n"
    except Exception as error:
        raise RuntimeStoreError("MALFORMED_RUNTIME_DOCUMENT") from error
    if not 1 <= len(raw) <= maximum_bytes:
        _fail("RUNTIME_DOCUMENT_SIZE_INVALID")
    return raw


def _strict_document(
    raw: bytes, *, maximum_bytes: int, label: str
) -> dict[str, object]:
    if not 1 <= len(raw) <= maximum_bytes:
        _fail("RUNTIME_DOCUMENT_SIZE_INVALID", label)

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _fail("NONCANONICAL_RUNTIME_DOCUMENT", f"duplicate key in {label}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        _fail("MALFORMED_RUNTIME_DOCUMENT", f"non-finite value {value} in {label}")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except RuntimeStoreError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise RuntimeStoreError("MALFORMED_RUNTIME_DOCUMENT", label) from error
    if type(value) is not dict:
        _fail("MALFORMED_RUNTIME_DOCUMENT", f"{label} root")
    try:
        canonical = canonical_json(value) + b"\n"
    except Exception as error:
        raise RuntimeStoreError("MALFORMED_RUNTIME_DOCUMENT", label) from error
    if raw != canonical:
        _fail("NONCANONICAL_RUNTIME_DOCUMENT", label)
    return value


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_existing_directory(path: Path, *, label: str) -> Path:
    supplied = path.expanduser()
    if not supplied.is_absolute():
        supplied = Path(os.path.abspath(supplied))
    try:
        metadata = supplied.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            _fail("UNSAFE_RUNTIME_PATH", label)
        resolved = supplied.resolve(strict=True)
    except RuntimeStoreError:
        raise
    except OSError as error:
        raise RuntimeStoreError("UNSAFE_RUNTIME_PATH", label) from error
    # This also rejects a symlink in an ancestor component.  Runtime data has a
    # single canonical spelling, so aliases cannot acquire a second lock.
    if resolved != supplied:
        _fail("UNSAFE_RUNTIME_PATH", f"noncanonical {label}")
    return resolved


def _safe_directory(path: Path, *, parent: Path, label: str) -> Path:
    if path.parent != parent:
        _fail("UNSAFE_RUNTIME_PATH", label)
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            _fail("UNSAFE_RUNTIME_PATH", label)
        if path.resolve(strict=True) != path:
            _fail("UNSAFE_RUNTIME_PATH", label)
    except RuntimeStoreError:
        raise
    except OSError as error:
        raise RuntimeStoreError("UNSAFE_RUNTIME_PATH", label) from error
    return path


def _mkdir_private(path: Path, *, parent: Path) -> None:
    if path.parent != parent:
        _fail("UNSAFE_RUNTIME_PATH", str(path))
    try:
        os.mkdir(path, 0o700)
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(parent)
    except FileExistsError as error:
        try:
            occupied = path.lstat()
        except OSError:
            occupied = None
        if occupied is not None and stat.S_ISLNK(occupied.st_mode):
            raise RuntimeStoreError("UNSAFE_RUNTIME_PATH", str(path)) from error
        raise RuntimeStoreError("RUNTIME_PATH_OCCUPIED", str(path)) from error
    except RuntimeStoreError:
        raise
    except OSError as error:
        raise RuntimeStoreError("RUNTIME_LAYOUT_FAILED", str(path)) from error


def _safe_file_bytes(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            _fail("UNSAFE_RUNTIME_PATH", label)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or not 1 <= opened.st_size <= maximum_bytes
            ):
                _fail("UNSAFE_RUNTIME_PATH", label)
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1_048_576, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != opened.st_size or len(raw) > maximum_bytes:
                _fail("RUNTIME_DOCUMENT_SIZE_INVALID", label)
            return raw
        finally:
            os.close(descriptor)
    except RuntimeStoreError:
        raise
    except FileNotFoundError:
        raise
    except OSError as error:
        raise RuntimeStoreError("RUNTIME_READ_FAILED", label) from error


def _publish_exclusive(
    path: Path,
    raw: bytes,
    *,
    mode: int = 0o600,
    idempotent: bool,
    conflict_code: str,
) -> None:
    """Publish complete bytes without exposing a partially written target.

    ``mkstemp`` provides O_EXCL creation for the staged inode.  The hard-link
    publication is itself no-replace: it either installs that complete inode
    under ``path`` or reports that the destination already exists.
    """

    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
    except OSError as error:
        raise RuntimeStoreError("RUNTIME_WRITE_FAILED", str(path)) from error
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            if idempotent:
                try:
                    existing = _safe_file_bytes(
                        path,
                        maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES,
                        label=str(path),
                    )
                except FileNotFoundError:
                    # The occupant vanished between link and read.  Do not
                    # retry a terminal publication under ambiguous ownership.
                    raise RuntimeStoreError(conflict_code, str(path)) from error
                if existing == raw:
                    return
            raise RuntimeStoreError(conflict_code, str(path)) from error
        _fsync_directory(path.parent)
    except RuntimeStoreError:
        raise
    except OSError as error:
        raise RuntimeStoreError("RUNTIME_WRITE_FAILED", str(path)) from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _replace_atomic(path: Path, raw: bytes) -> None:
    if path.is_symlink():
        _fail("UNSAFE_RUNTIME_PATH", str(path))
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
    except OSError as error:
        raise RuntimeStoreError("RUNTIME_WRITE_FAILED", str(path)) from error
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            _fail("UNSAFE_RUNTIME_PATH", str(path))
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except RuntimeStoreError:
        raise
    except OSError as error:
        raise RuntimeStoreError("RUNTIME_WRITE_FAILED", str(path)) from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True, slots=True)
class SessionPaths:
    root: Path
    private: Path
    input: Path
    workspace: Path
    cache: Path
    evidence: Path
    state: Path
    receipt: Path


class RuntimeClaim:
    """A nonblocking, process-wide claim on one cohort runtime."""

    def __init__(self, cohort_root: str | os.PathLike[str]) -> None:
        self.cohort_root = _canonical_existing_directory(
            Path(cohort_root), label="cohort root"
        )
        self.path = self.cohort_root / ".runtime.lock"
        self._descriptor: int | None = None

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> "RuntimeClaim":
        if self._descriptor is not None:
            _fail("RUNTIME_CLAIM_ALREADY_HELD")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            named = self.path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(named.st_mode)
                or metadata.st_dev != named.st_dev
                or metadata.st_ino != named.st_ino
            ):
                _fail("UNSAFE_RUNTIME_PATH", "runtime claim")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise RuntimeStoreError("RUNTIME_CLAIM_HELD") from error
                raise
            # Re-check after acquiring: replacing the pathname while a waiter
            # was blocked must not create two independently locked inodes.
            named = self.path.lstat()
            if (
                stat.S_ISLNK(named.st_mode)
                or metadata.st_dev != named.st_dev
                or metadata.st_ino != named.st_ino
            ):
                _fail("UNSAFE_RUNTIME_PATH", "runtime claim replaced")
            os.fsync(descriptor)
            _fsync_directory(self.cohort_root)
        except RuntimeStoreError:
            try:
                os.close(descriptor)
            except (NameError, OSError):
                pass
            raise
        except OSError as error:
            try:
                os.close(descriptor)
            except (NameError, OSError):
                pass
            raise RuntimeStoreError("RUNTIME_CLAIM_FAILED") from error
        self._descriptor = descriptor
        return self

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "RuntimeClaim":
        return self.acquire()

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.release()


class RuntimeStore:
    """Durable layout and canonical documents for one cohort runtime."""

    def __init__(self, cohort_root: str | os.PathLike[str]) -> None:
        self.cohort_root = _canonical_existing_directory(
            Path(cohort_root), label="cohort root"
        )
        self.runtime_root = self.cohort_root / "runtime"
        self.launch_path = self.runtime_root / "launch.json"
        self.sessions_root = self.runtime_root / "sessions"
        self.settlement_path = self.runtime_root / "settlement.json"

    @staticmethod
    def _validate_session_ids(session_ids: Sequence[str]) -> tuple[str, ...]:
        if isinstance(session_ids, (str, bytes)) or not isinstance(
            session_ids, Sequence
        ):
            _fail("MALFORMED_SESSION_SET")
        selected = tuple(
            _identifier(session_id, label="session_id") for session_id in session_ids
        )
        if not 1 <= len(selected) <= MAXIMUM_SESSIONS or len(set(selected)) != len(
            selected
        ):
            _fail("MALFORMED_SESSION_SET")
        return selected

    @staticmethod
    def _launch_session_ids(launch: Mapping[str, object]) -> tuple[str, ...]:
        candidates: list[tuple[str, ...]] = []
        direct = launch.get("session_ids")
        if direct is not None:
            if type(direct) is not list:
                _fail("MALFORMED_LAUNCH_SESSION_SET", "session_ids")
            candidates.append(RuntimeStore._validate_session_ids(direct))
        sessions = launch.get("sessions")
        if sessions is not None:
            if type(sessions) is not list:
                _fail("MALFORMED_LAUNCH_SESSION_SET", "sessions")
            extracted: list[object] = []
            for row in sessions:
                if type(row) is str:
                    extracted.append(row)
                elif type(row) is dict:
                    extracted.append(row.get("session_id"))
                else:
                    _fail("MALFORMED_LAUNCH_SESSION_SET", "sessions")
            candidates.append(RuntimeStore._validate_session_ids(extracted))
        if not candidates or any(candidate != candidates[0] for candidate in candidates):
            _fail("MALFORMED_LAUNCH_SESSION_SET")
        return candidates[0]

    @staticmethod
    def _validate_launch_value(launch: Mapping[str, object]) -> None:
        if (
            set(launch) != _LAUNCH_FIELDS
            or launch.get("schema") != RUNTIME_LAUNCH_SCHEMA
        ):
            _fail("MALFORMED_RUNTIME_LAUNCH", "schema")
        for label in (
            "plan_sha256",
            "briefing_sha256",
            "safety_profile_sha256",
            "core_lock_sha256",
            "backend_sha256",
            "publication_sha256",
            "required_readiness_sha256",
            "verifier_kit_sha256",
            "agenda_arm_sha256",
        ):
            digest = launch.get(label)
            if type(digest) is not str or _SHA256.fullmatch(digest) is None:
                _fail("MALFORMED_RUNTIME_LAUNCH", label)
        created_at = launch.get("created_at")
        if not _bounded_utc_timestamp(created_at):
            _fail("MALFORMED_RUNTIME_LAUNCH", "created_at")
        _identifier(launch.get("cohort_id"), label="cohort_id")
        _identifier(launch.get("world_id"), label="world_id")
        _identifier(launch.get("safety_profile"), label="safety_profile")
        world_ref = launch.get("world_ref")
        if (
            type(world_ref) is not str
            or not world_ref.startswith("refs/")
            or len(world_ref.encode("utf-8")) > 512
            or any(selected in world_ref for selected in ("\x00", "\r", "\n"))
        ):
            _fail("MALFORMED_RUNTIME_LAUNCH", "world_ref")
        snapshot = launch.get("base_snapshot_ref")
        if type(snapshot) is not str or _SNAPSHOT_REF.fullmatch(snapshot) is None:
            _fail("MALFORMED_RUNTIME_LAUNCH", "base_snapshot_ref")

        backend = launch.get("backend")
        try:
            if type(backend) is not dict or set(backend) != {
                "name",
                "protocol",
                "public_config",
            }:
                raise RuntimeContractError("MALFORMED_BACKEND_IDENTITY")
            rebuilt_backend = BackendIdentity(
                name=backend.get("name"),  # type: ignore[arg-type]
                protocol=backend.get("protocol"),  # type: ignore[arg-type]
                public_config=backend.get("public_config"),  # type: ignore[arg-type]
            )
        except (RuntimeContractError, TypeError, ValueError) as error:
            raise RuntimeStoreError(
                "MALFORMED_RUNTIME_LAUNCH", "backend identity"
            ) from error
        if (
            rebuilt_backend.to_value() != backend
            or rebuilt_backend.sha256 != launch.get("backend_sha256")
        ):
            _fail("MALFORMED_RUNTIME_LAUNCH", "backend identity")

        publication = launch.get("publication")
        if (
            type(publication) is not dict
            or set(publication)
            != {"schema", "mode", "protocol", "public_config"}
            or publication.get("schema")
            != "PMW_RUNTIME_PUBLICATION_IDENTITY_1"
            or type(publication.get("mode")) is not str
            or type(publication.get("protocol")) is not str
            or type(publication.get("public_config")) is not dict
            or hashlib.sha256(canonical_json(publication)).hexdigest()
            != launch.get("publication_sha256")
        ):
            _fail("MALFORMED_RUNTIME_LAUNCH", "publication identity")
        try:
            rebuilt_publication = BackendIdentity(
                name=publication["mode"],  # type: ignore[arg-type,index]
                protocol=publication["protocol"],  # type: ignore[arg-type,index]
                public_config=publication["public_config"],  # type: ignore[arg-type,index]
            )
        except (RuntimeContractError, TypeError, ValueError) as error:
            raise RuntimeStoreError(
                "MALFORMED_RUNTIME_LAUNCH", "publication identity"
            ) from error
        if (
            rebuilt_publication.name != publication["mode"]
            or rebuilt_publication.protocol != publication["protocol"]
            or rebuilt_publication.public_config != publication["public_config"]
        ):
            _fail("MALFORMED_RUNTIME_LAUNCH", "publication identity")

        session_ids = RuntimeStore._launch_session_ids(launch)
        concurrency = launch.get("concurrency")
        if (
            type(concurrency) is not int
            or not 1 <= concurrency <= len(session_ids)
        ):
            _fail("MALFORMED_RUNTIME_LAUNCH", "concurrency")
        limits = launch.get("limits")
        if type(limits) is not dict or set(limits) != {
            "startup_seconds",
            "session_wall_seconds",
            "stop_grace_seconds",
        }:
            _fail("MALFORMED_RUNTIME_LAUNCH", "limits")
        for label in ("startup_seconds", "stop_grace_seconds"):
            selected = limits[label]
            if (
                type(selected) not in {int, float}
                or not math.isfinite(float(selected))
                or selected <= 0
            ):
                _fail("MALFORMED_RUNTIME_LAUNCH", f"limits.{label}")
        if limits["stop_grace_seconds"] > MAXIMUM_STOP_GRACE_SECONDS:  # type: ignore[operator]
            _fail("MALFORMED_RUNTIME_LAUNCH", "limits.stop_grace_seconds")
        wall = limits["session_wall_seconds"]
        if wall is not None and (
            type(wall) not in {int, float}
            or not math.isfinite(float(wall))
            or wall <= 0
        ):
            _fail("MALFORMED_RUNTIME_LAUNCH", "limits.session_wall_seconds")

        raw_context = launch.get("context_window_policy")
        if type(raw_context) is not dict or set(raw_context) != {
            "schema",
            "semantics",
            "default_tokens",
            "session_overrides",
            "effective_sessions",
            "unset_semantics",
        }:
            _fail("MALFORMED_RUNTIME_LAUNCH", "context_window_policy")
        raw_overrides = raw_context.get("session_overrides")
        if type(raw_overrides) is not list:
            _fail("MALFORMED_RUNTIME_LAUNCH", "context_window_policy")
        overrides: dict[str, int] = {}
        for row in raw_overrides:
            if type(row) is not dict or set(row) != {
                "session_id",
                "context_window_tokens",
            }:
                _fail("MALFORMED_RUNTIME_LAUNCH", "context_window_policy")
            override_session = row.get("session_id")
            if type(override_session) is not str or override_session in overrides:
                _fail("MALFORMED_RUNTIME_LAUNCH", "context_window_policy")
            overrides[override_session] = row.get("context_window_tokens")  # type: ignore[assignment]
        try:
            rebuilt_context = ContextWindowPolicy(
                default_tokens=raw_context.get("default_tokens"),  # type: ignore[arg-type]
                session_overrides=overrides,
            )
            context_control = ContextWindowControl(
                launch.get("backend_context_window_control")
            )
        except (TypeError, ValueError) as error:
            raise RuntimeStoreError(
                "MALFORMED_RUNTIME_LAUNCH", "context_window_policy"
            ) from error
        if (
            rebuilt_context.bind(session_ids) != raw_context
            or (
                rebuilt_context.configured
                and context_control
                is not ContextWindowControl.NATIVE_MODEL_WINDOW
            )
        ):
            _fail("MALFORMED_RUNTIME_LAUNCH", "context_window_policy")
        _validate_launch_verifier_kit(launch)
        _validate_launch_agenda_arm(launch)
        if launch.get("host_policy") != runtime_host_policy_value():
            _fail("MALFORMED_RUNTIME_LAUNCH", "host_policy")
        readiness = launch.get("required_readiness")
        if (
            type(readiness) is not dict
            or set(readiness) != {"schema", "checks"}
            or readiness.get("schema") != "PMW_RUNTIME_REQUIRED_READINESS_1"
            or type(readiness.get("checks")) is not list
            or len(readiness["checks"]) > 22  # type: ignore[arg-type]
            or hashlib.sha256(canonical_json(readiness)).hexdigest()
            != launch.get("required_readiness_sha256")
        ):
            _fail("MALFORMED_RUNTIME_LAUNCH", "required_readiness")
        readiness_names: set[str] = set()
        for row in readiness["checks"]:  # type: ignore[index]
            if (
                type(row) is not dict
                or set(row) != {"name", "evidence"}
                or type(row.get("name")) is not str
                or not row["name"]
                or len(row["name"].encode("utf-8")) > 64
                or row["name"] in readiness_names
                or type(row.get("evidence")) is not dict
                or len(canonical_json(row["evidence"])) > 2_048
            ):
                _fail("MALFORMED_RUNTIME_LAUNCH", "required_readiness")
            readiness_names.add(row["name"])

    def _runtime_directories(self) -> None:
        _safe_directory(
            self.runtime_root, parent=self.cohort_root, label="runtime root"
        )
        _safe_directory(
            self.sessions_root, parent=self.runtime_root, label="sessions root"
        )

    def create_launch(
        self,
        launch: Mapping[str, object],
        *,
        session_ids: Sequence[str],
    ) -> str:
        """Create the complete private layout, then publish an immutable launch."""

        if type(launch) is not dict:
            _fail("MALFORMED_RUNTIME_DOCUMENT", "launch")
        self._validate_launch_value(launch)
        selected_ids = self._validate_session_ids(session_ids)
        if self._launch_session_ids(launch) != selected_ids:
            _fail("LAUNCH_SESSION_SET_MISMATCH")
        raw = _canonical_document(
            launch, maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES
        )
        launch_sha256 = hashlib.sha256(raw).hexdigest()

        # The top-level mkdir is the exclusive creation point.  A crash leaves
        # a visible incomplete runtime that requires explicit inspection; a
        # later launcher never silently adopts or overwrites it.
        _mkdir_private(self.runtime_root, parent=self.cohort_root)
        _mkdir_private(self.sessions_root, parent=self.runtime_root)
        for session_id in selected_ids:
            root = self.sessions_root / session_id
            _mkdir_private(root, parent=self.sessions_root)
            for name in ("private", "input", "workspace", "cache", "evidence"):
                _mkdir_private(root / name, parent=root)
            initial_state = {
                "schema": RUNTIME_STATE_SCHEMA,
                "session_id": session_id,
                "launch_sha256": launch_sha256,
                "state": "PLANNED",
            }
            _publish_exclusive(
                root / "state.json",
                _canonical_document(initial_state, maximum_bytes=MAXIMUM_STATE_BYTES),
                idempotent=False,
                conflict_code="STATE_ALREADY_EXISTS",
            )
        _fsync_directory(self.sessions_root)
        _publish_exclusive(
            self.launch_path,
            raw,
            idempotent=False,
            conflict_code="LAUNCH_ALREADY_EXISTS",
        )
        return launch_sha256

    def session_paths(self, session_id: str) -> SessionPaths:
        selected = _identifier(session_id, label="session_id")
        self._runtime_directories()
        root = _safe_directory(
            self.sessions_root / selected,
            parent=self.sessions_root,
            label=f"session {selected}",
        )
        private = _safe_directory(root / "private", parent=root, label="private")
        input_root = _safe_directory(root / "input", parent=root, label="input")
        workspace = _safe_directory(
            root / "workspace", parent=root, label="workspace"
        )
        cache = _safe_directory(root / "cache", parent=root, label="cache")
        evidence = _safe_directory(root / "evidence", parent=root, label="evidence")
        return SessionPaths(
            root=root,
            private=private,
            input=input_root,
            workspace=workspace,
            cache=cache,
            evidence=evidence,
            state=root / "state.json",
            receipt=root / "receipt.json",
        )

    def write_input_file(
        self,
        session_id: str,
        name: str,
        content: bytes,
        *,
        mode: int = 0o600,
    ) -> Path:
        """Exclusively publish one bounded input under a session's input root."""

        if type(name) is not str or _INPUT_NAME.fullmatch(name) is None:
            _fail("UNSAFE_INPUT_NAME")
        if type(content) is not bytes or len(content) > MAXIMUM_INPUT_BYTES:
            _fail("RUNTIME_INPUT_SIZE_INVALID", name)
        if mode not in {0o400, 0o600}:
            _fail("UNSAFE_INPUT_MODE", oct(mode))
        destination = self.session_paths(session_id).input / name
        _publish_exclusive(
            destination,
            content,
            mode=mode,
            idempotent=False,
            conflict_code="RUNTIME_INPUT_ALREADY_EXISTS",
        )
        return destination

    def read_launch(self) -> dict[str, object]:
        self._runtime_directories()
        try:
            raw = _safe_file_bytes(
                self.launch_path,
                maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES,
                label="launch",
            )
        except FileNotFoundError as error:
            raise RuntimeStoreError("RUNTIME_NOT_LAUNCHED") from error
        value = _strict_document(
            raw, maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES, label="launch"
        )
        self._validate_launch_value(value)
        self._launch_session_ids(value)
        return value

    def launch_sha256(self) -> str:
        try:
            raw = _safe_file_bytes(
                self.launch_path,
                maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES,
                label="launch",
            )
        except FileNotFoundError as error:
            raise RuntimeStoreError("RUNTIME_NOT_LAUNCHED") from error
        _strict_document(
            raw, maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES, label="launch"
        )
        return hashlib.sha256(raw).hexdigest()

    def write_state(self, session_id: str, state_value: Mapping[str, object]) -> None:
        if type(state_value) is not dict:
            _fail("MALFORMED_RUNTIME_DOCUMENT", "state")
        paths = self.session_paths(session_id)
        expected_launch = self.launch_sha256()
        if state_value.get("schema") != RUNTIME_STATE_SCHEMA:
            _fail("MALFORMED_SESSION_STATE", "schema")
        if state_value.get("session_id") != session_id:
            _fail("MALFORMED_SESSION_STATE", "session_id")
        if state_value.get("launch_sha256") != expected_launch:
            _fail("MALFORMED_SESSION_STATE", "launch_sha256")
        if type(state_value.get("state")) is not str:
            _fail("MALFORMED_SESSION_STATE", "state")
        raw = _canonical_document(state_value, maximum_bytes=MAXIMUM_STATE_BYTES)
        _replace_atomic(paths.state, raw)

    def read_state(self, session_id: str) -> dict[str, object]:
        path = self.session_paths(session_id).state
        try:
            raw = _safe_file_bytes(
                path, maximum_bytes=MAXIMUM_STATE_BYTES, label=f"state {session_id}"
            )
        except FileNotFoundError as error:
            raise RuntimeStoreError("SESSION_STATE_UNAVAILABLE", session_id) from error
        value = _strict_document(
            raw, maximum_bytes=MAXIMUM_STATE_BYTES, label=f"state {session_id}"
        )
        if (
            value.get("schema") != RUNTIME_STATE_SCHEMA
            or value.get("session_id") != session_id
            or value.get("launch_sha256") != self.launch_sha256()
            or type(value.get("state")) is not str
        ):
            _fail("MALFORMED_SESSION_STATE", session_id)
        return value

    def _validate_receipt_value(
        self, session_id: str, value: Mapping[str, object]
    ) -> None:
        launch = self.read_launch()
        if (
            set(value) != _RECEIPT_FIELDS
            or value.get("schema") != RUNTIME_RECEIPT_SCHEMA
            or value.get("session_id") != session_id
            or value.get("launch_sha256") != self.launch_sha256()
            or value.get("plan_sha256") != launch.get("plan_sha256")
            or value.get("backend_sha256") != launch.get("backend_sha256")
            or value.get("cohort_id") != launch.get("cohort_id")
            or value.get("world_id") != launch.get("world_id")
            or value.get("world_ref") != launch.get("world_ref")
            or value.get("base_snapshot_ref") != launch.get("base_snapshot_ref")
            or value.get("status") not in _TERMINAL_STATUSES
            or type(value.get("terminal_reason")) is not str
            or _TERMINAL_REASON.fullmatch(value["terminal_reason"]) is None  # type: ignore[arg-type]
            or (
                value.get("started_at") is not None
                and not _bounded_utc_timestamp(value.get("started_at"))
            )
            or not _bounded_utc_timestamp(value.get("finished_at"))
            or type(value.get("publications")) is not list
            or len(value["publications"]) > 64  # type: ignore[arg-type]
        ):
            _fail("MALFORMED_SESSION_RECEIPT", session_id)
        stopped = _validate_stop_proof(value.get("stop_proof"), session_id=session_id)
        succeeded = _validate_receipt_outcome(
            value.get("outcome"), session_id=session_id
        )
        _validate_receipt_error(value.get("error"), session_id=session_id)
        _validate_resource_guard(
            value.get("resource_guard"), session_id=session_id
        )
        # A durable receipt must never hold a token count whose epistemic
        # status is unstated, so the usage block is structurally required.
        if not usage_evidence_value_is_valid(value.get("usage")):
            _fail("MALFORMED_SESSION_RECEIPT", f"usage {session_id}")
        _validate_verifier_kit_evidence(
            value.get("verifier_kit"), session_id=session_id, launch=launch
        )
        agenda_rejected = _validate_agenda_arm_evidence(
            value.get("agenda_arm"), session_id=session_id, launch=launch
        )
        context_window = value.get("context_window")
        launch_context = launch.get("context_window_policy")
        effective_tokens: object = None
        if type(launch_context) is dict:
            effective = launch_context.get("effective_sessions")
            if type(effective) is list:
                for row in effective:
                    if type(row) is dict and row.get("session_id") == session_id:
                        effective_tokens = row.get("context_window_tokens")
                        break
        if (
            type(context_window) is not dict
            or set(context_window) != _RECEIPT_CONTEXT_WINDOW_FIELDS
            or context_window.get("semantics") != CONTEXT_WINDOW_SEMANTICS
            or context_window.get("configured_tokens") != effective_tokens
            or context_window.get("backend_control")
            != launch.get("backend_context_window_control")
            or context_window.get("strict_pre_http_input_gate") is not False
        ):
            _fail("MALFORMED_SESSION_RECEIPT", f"context_window {session_id}")
        status = value["status"]
        outcome = value.get("outcome")
        contribution_count = (
            outcome.get("contribution_count")
            if type(outcome) is dict
            else None
        )
        publications = value["publications"]
        publication = launch.get("publication")
        publication_mode = (
            publication.get("mode")
            if type(publication) is dict
            else None
        )
        resource_guard = value["resource_guard"]
        if type(resource_guard) is not dict:
            raise AssertionError("validated resource guard is not an object")
        resource_terminal = resource_guard.get("terminal_event")
        accounting_unknown = (
            type(resource_terminal) is dict
            and resource_terminal.get("code") == "RESOURCE_ACCOUNTING_UNCERTAIN"
            and resource_terminal.get("uncertain") is True
        )
        if (
            (
                status == "SUCCEEDED"
                and (
                    stopped is not True
                    or succeeded is not True
                    or value.get("error") is not None
                )
            )
            or (status == "CANCELLED" and stopped is not True)
            or (status == "FAILED" and stopped is False)
            or (
                value.get("started_at") is not None
                and status != "UNKNOWN"
                and stopped is not True
            )
            or (
                publication_mode == "DISABLED"
                and len(publications) != 0  # type: ignore[arg-type]
            )
            or (
                (
                    contribution_count is None
                    and (len(publications) != 0 or agenda_rejected != 0)  # type: ignore[arg-type]
                )
                or (
                    type(contribution_count) is int
                    and len(publications) + agenda_rejected  # type: ignore[arg-type]
                    > contribution_count
                )
            )
            or (
                status == "SUCCEEDED"
                and (
                    type(contribution_count) is not int
                    # Every contribution of a successful session is accounted
                    # for exactly once: either it was published, or the agenda
                    # arm rejected it and recorded the verdict.  A rejection is
                    # a research event, so it must not leave a hole here.
                    or len(publications) + agenda_rejected  # type: ignore[arg-type]
                    != contribution_count
                    or (
                        publication_mode == "DISABLED"
                        and contribution_count != 0
                    )
                )
            )
            or (
                status == "UNKNOWN"
                and stopped is True
                and (
                    value.get("terminal_reason")
                    != "RESOURCE_ACCOUNTING_UNCERTAIN"
                    or not accounting_unknown
                )
            )
        ):
            _fail("MALFORMED_SESSION_RECEIPT", f"terminal invariants {session_id}")

    def write_receipt(
        self, session_id: str, receipt: Mapping[str, object]
    ) -> str:
        if type(receipt) is not dict:
            _fail("MALFORMED_RUNTIME_DOCUMENT", "receipt")
        paths = self.session_paths(session_id)
        self._validate_receipt_value(session_id, receipt)
        raw = _canonical_document(
            receipt, maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES
        )
        _publish_exclusive(
            paths.receipt,
            raw,
            idempotent=True,
            conflict_code="SESSION_RECEIPT_CONFLICT",
        )
        return hashlib.sha256(raw).hexdigest()

    def read_receipt(self, session_id: str) -> dict[str, object] | None:
        path = self.session_paths(session_id).receipt
        try:
            raw = _safe_file_bytes(
                path,
                maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES,
                label=f"receipt {session_id}",
            )
        except FileNotFoundError:
            return None
        value = _strict_document(
            raw,
            maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES,
            label=f"receipt {session_id}",
        )
        self._validate_receipt_value(session_id, value)
        return value

    def receipt_sha256(self, session_id: str) -> str | None:
        path = self.session_paths(session_id).receipt
        try:
            raw = _safe_file_bytes(
                path,
                maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES,
                label=f"receipt {session_id}",
            )
        except FileNotFoundError:
            return None
        value = _strict_document(
            raw,
            maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES,
            label=f"receipt {session_id}",
        )
        self._validate_receipt_value(session_id, value)
        return hashlib.sha256(raw).hexdigest()

    def _validate_settlement_value(
        self, settlement: Mapping[str, object]
    ) -> None:
        launch = self.read_launch()
        session_ids = self._launch_session_ids(launch)
        launch_sha256 = self.launch_sha256()
        if (
            set(settlement) != _SETTLEMENT_FIELDS
            or settlement.get("schema") != RUNTIME_SETTLEMENT_SCHEMA
        ):
            _fail("MALFORMED_SETTLEMENT", "schema")
        if settlement.get("launch_sha256") != launch_sha256:
            _fail("MALFORMED_SETTLEMENT", "launch_sha256")
        for label in ("plan_sha256", "cohort_id"):
            if settlement.get(label) != launch.get(label):
                _fail("MALFORMED_SETTLEMENT", label)
        finished_at = settlement.get("finished_at")
        if not _bounded_utc_timestamp(finished_at):
            _fail("MALFORMED_SETTLEMENT", "finished_at")
        rows = settlement.get("receipts")
        if type(rows) is not list or len(rows) != len(session_ids):
            _fail("MALFORMED_SETTLEMENT", "receipts")
        actual_statuses: list[str] = []
        for expected_id, row in zip(session_ids, rows, strict=True):
            if (
                type(row) is not dict
                or set(row) != _SETTLEMENT_RECEIPT_FIELDS
                or row.get("session_id") != expected_id
            ):
                _fail("MALFORMED_SETTLEMENT", "receipt order")
            digest = row.get("receipt_sha256")
            if type(digest) is not str or _SHA256.fullmatch(digest) is None:
                _fail("MALFORMED_SETTLEMENT", f"receipt digest {expected_id}")
            actual = self.receipt_sha256(expected_id)
            if actual is None:
                _fail("SETTLEMENT_INCOMPLETE", expected_id)
            if actual != digest:
                _fail("SETTLEMENT_RECEIPT_MISMATCH", expected_id)
            receipt = self.read_receipt(expected_id)
            if receipt is None or row.get("status") != receipt.get("status"):
                _fail("SETTLEMENT_RECEIPT_MISMATCH", f"status {expected_id}")
            actual_statuses.append(str(receipt["status"]))

        expected_counts = {
            status: actual_statuses.count(status)
            for status in sorted(_TERMINAL_STATUSES)
        }
        if settlement.get("counts") != expected_counts:
            _fail("MALFORMED_SETTLEMENT", "counts")
        outcome = settlement.get("outcome")
        expected_outcome = (
            "UNSAFE"
            if "UNKNOWN" in actual_statuses
            else "CANCELLED"
            if "CANCELLED" in actual_statuses
            else "COMPLETED_WITH_FAILURES"
            if "FAILED" in actual_statuses
            else "SUCCEEDED"
        )
        if outcome != expected_outcome:
            _fail("MALFORMED_SETTLEMENT", "outcome/status mismatch")

    def write_settlement(self, settlement: Mapping[str, object]) -> str:
        """Publish a complete receipt index after verifying every file digest."""

        if type(settlement) is not dict:
            _fail("MALFORMED_RUNTIME_DOCUMENT", "settlement")
        self._validate_settlement_value(settlement)
        raw = _canonical_document(
            settlement, maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES
        )
        _publish_exclusive(
            self.settlement_path,
            raw,
            idempotent=True,
            conflict_code="SETTLEMENT_CONFLICT",
        )
        return hashlib.sha256(raw).hexdigest()

    def read_settlement(self) -> dict[str, object] | None:
        self._runtime_directories()
        try:
            raw = _safe_file_bytes(
                self.settlement_path,
                maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES,
                label="settlement",
            )
        except FileNotFoundError:
            return None
        value = _strict_document(
            raw, maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES, label="settlement"
        )
        self._validate_settlement_value(value)
        return value

    def read_status(self) -> dict[str, object]:
        """Return a compact recovery/CLI projection without embedding receipts."""

        launch = self.read_launch()
        launch_sha256 = self.launch_sha256()
        sessions: list[dict[str, object]] = []
        for session_id in self._launch_session_ids(launch):
            state = self.read_state(session_id)
            receipt = self.read_receipt(session_id)
            sessions.append(
                {
                    "session_id": session_id,
                    "state": state.get("state"),
                    "receipt_status": (
                        None if receipt is None else receipt.get("status")
                    ),
                    "receipt_sha256": self.receipt_sha256(session_id),
                }
            )
        settlement = self.read_settlement()
        settlement_sha256: str | None = None
        if settlement is not None:
            raw = _safe_file_bytes(
                self.settlement_path,
                maximum_bytes=MAXIMUM_RUNTIME_DOCUMENT_BYTES,
                label="settlement",
            )
            settlement_sha256 = hashlib.sha256(raw).hexdigest()
        return {
            "launch_sha256": launch_sha256,
            "sessions": sessions,
            "settled": settlement is not None,
            "settlement_sha256": settlement_sha256,
        }
