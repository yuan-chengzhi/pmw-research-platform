"""Thin, session-neutral adapter over the pinned PMW Git world broker."""

from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Callable, NoReturn, TYPE_CHECKING

from .records import ResearchContribution, ResearchRecord, canonical_json

if TYPE_CHECKING:
    from ..sessions.model import SessionSpec


try:  # The deployment source cache supplies the commit pinned by core-lock.json.
    from pmw_r2.pi_agent_tools import (
        AgentToolBinding,
        PMW_PROPOSE,
        PiAgentToolDispatcher,
        new_channel_token,
        sign_tool_envelope,
    )
    from pmw_r2.platform_admission import (
        AdmissionError,
        GitWorldBroker,
        audit_git_world,
    )
except ImportError as exc:  # Records remain usable without the optional core.
    AgentToolBinding = None  # type: ignore[assignment]
    PiAgentToolDispatcher = None  # type: ignore[assignment]
    GitWorldBroker = None  # type: ignore[assignment]
    AdmissionError = Exception  # type: ignore[assignment,misc]
    PMW_PROPOSE = "pmw_propose"
    new_channel_token = None  # type: ignore[assignment]
    sign_tool_envelope = None  # type: ignore[assignment]
    audit_git_world = None  # type: ignore[assignment]
    _CORE_IMPORT_ERROR: ImportError | None = exc
else:
    _CORE_IMPORT_ERROR = None


DEFAULT_WORLD_REF = "refs/pmw/research-world"
_SNAPSHOT_REF = re.compile(r"^snapshot/sha256/[0-9a-f]{64}$")
_ADMISSION_REF = re.compile(r"^admission/sha256/[0-9a-f]{64}$")
_RECEIPT_REF = re.compile(r"^receipt/sha256/[0-9a-f]{64}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_WORLD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+=-]{0,1023}$")
_WORLD_REF = re.compile(r"^refs/pmw/[A-Za-z0-9][A-Za-z0-9._/-]{0,240}$")


class ResearchWorldError(RuntimeError):
    """Stable boundary error emitted by the generic world adapter."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _fail(code: str, detail: str = "") -> NoReturn:
    raise ResearchWorldError(code, detail)


def _require_core() -> None:
    if _CORE_IMPORT_ERROR is not None:
        raise ResearchWorldError(
            "PMW_CORE_UNAVAILABLE",
            "install or expose the commit pinned by pmw_platform/locks/core-lock.json",
        ) from _CORE_IMPORT_ERROR


def _reference(value: object, *, label: str) -> str:
    if type(value) is not str or _REFERENCE.fullmatch(value) is None:
        _fail("MALFORMED_WRITER_AUTHORITY", label)
    return value


def _snapshot(value: object, *, label: str) -> str:
    if type(value) is not str or _SNAPSHOT_REF.fullmatch(value) is None:
        _fail("MALFORMED_SNAPSHOT_REF", label)
    return value


def _admission(value: object, *, label: str) -> str:
    if type(value) is not str or _ADMISSION_REF.fullmatch(value) is None:
        _fail("MALFORMED_ADMISSION_REF", label)
    return value


def _json_clone(value: object) -> object:
    return json.loads(canonical_json(value).decode("utf-8"))


def _decode_json_or_text(raw: bytes) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> NoReturn:
        raise ValueError("non-finite JSON")

    def finite_float(value: str) -> float:
        selected = float(value)
        if not math.isfinite(selected):
            raise ValueError("non-finite JSON")
        return selected

    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_float=finite_float,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        return raw.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class PmwWriterAuthority:
    """Existing PMW capability used by the platform's trusted host writer.

    This is durable world configuration, not a session credential.  A future
    runtime must authenticate each session to an outer broker. Its verified
    identity will be recorded in :class:`ResearchRecord` while the stable host
    performs the PMW admission.
    """

    channel_ref: str
    invocation_ref: str
    process_ref: str
    principal_ref: str
    episode_ref: str
    capability_ref: str
    scope_ref: str
    policy_ref: str
    policy_fingerprint: str
    maximum_calls: int = 1_000_000
    maximum_delivery_attempts: int = 1_000_000
    maximum_content_bytes: int = 65_536
    maximum_parent_refs: int = 16

    def __post_init__(self) -> None:
        for label in (
            "channel_ref",
            "invocation_ref",
            "process_ref",
            "principal_ref",
            "episode_ref",
            "capability_ref",
            "scope_ref",
            "policy_ref",
        ):
            _reference(getattr(self, label), label=label)
        if (
            type(self.policy_fingerprint) is not str
            or _FINGERPRINT.fullmatch(self.policy_fingerprint) is None
        ):
            _fail("MALFORMED_WRITER_AUTHORITY", "policy_fingerprint")
        for label in (
            "maximum_calls",
            "maximum_delivery_attempts",
            "maximum_content_bytes",
            "maximum_parent_refs",
        ):
            value = getattr(self, label)
            if type(value) is not int or value <= 0:
                _fail("MALFORMED_WRITER_AUTHORITY", label)
        if (
            self.maximum_delivery_attempts < self.maximum_calls
            or self.maximum_content_bytes > 65_536
            or self.maximum_parent_refs > 16
        ):
            _fail("MALFORMED_WRITER_AUTHORITY", "limits")

    def build_binding(self) -> Any:
        _require_core()
        assert AgentToolBinding is not None
        return AgentToolBinding(
            channel_ref=self.channel_ref,
            invocation_ref=self.invocation_ref,
            process_ref=self.process_ref,
            principal_ref=self.principal_ref,
            episode_ref=self.episode_ref,
            capability_ref=self.capability_ref,
            scope_ref=self.scope_ref,
            policy_ref=self.policy_ref,
            policy_fingerprint=self.policy_fingerprint,
            allowed_actions=frozenset({PMW_PROPOSE}),
            maximum_calls=self.maximum_calls,
            maximum_delivery_attempts=self.maximum_delivery_attempts,
            maximum_content_bytes=self.maximum_content_bytes,
            maximum_parent_refs=self.maximum_parent_refs,
        )


@dataclass(frozen=True)
class WorldAdmission:
    """One decoded, immutable PMW derived-view row."""

    admission_ref: str
    parent_refs: tuple[str, ...]
    content_bytes: bytes = field(repr=False)
    _admission_bytes: bytes = field(repr=False)
    _lineages_bytes: bytes = field(repr=False)

    @property
    def admission(self) -> dict[str, object]:
        value = json.loads(self._admission_bytes.decode("utf-8"))
        if type(value) is not dict:
            raise AssertionError("PMW admission is not an object")
        return value

    @property
    def lineages(self) -> list[object]:
        value = json.loads(self._lineages_bytes.decode("utf-8"))
        if type(value) is not list:
            raise AssertionError("PMW lineages are not a list")
        return value

    @property
    def content(self) -> object:
        return _decode_json_or_text(self.content_bytes)

    @property
    def schema(self) -> str | None:
        content = self.content
        if type(content) is not dict or type(content.get("schema")) is not str:
            return None
        return str(content["schema"])

    def to_value(self) -> dict[str, object]:
        return {
            "admission": self.admission,
            "content": self.content,
            "lineages": self.lineages,
        }


@dataclass(frozen=True)
class PublishResult:
    admission_ref: str
    base_snapshot_ref: str
    snapshot_ref: str
    receipt_ref: str
    content_sha256: str

    def to_value(self) -> dict[str, object]:
        return {
            "admission_ref": self.admission_ref,
            "base_snapshot_ref": self.base_snapshot_ref,
            "snapshot_ref": self.snapshot_ref,
            "receipt_ref": self.receipt_ref,
            "content_sha256": self.content_sha256,
        }


class BoundResearchSession:
    """Content-only publish surface; a future runtime supplies verified identity."""

    def __init__(
        self,
        world: "ResearchWorld",
        spec: "SessionSpec",
        *,
        artifact_exists: Callable[[str], bool] | None,
    ) -> None:
        self._world = world
        self.spec = spec
        self._artifact_exists = artifact_exists

    def publish(self, contribution: ResearchContribution) -> PublishResult:
        if not isinstance(contribution, ResearchContribution):
            _fail("MALFORMED_RESEARCH_CONTRIBUTION")
        if contribution.artifact_refs:
            if self._artifact_exists is None:
                _fail("ARTIFACT_STORE_UNAVAILABLE")
            for reference in contribution.artifact_refs:
                if not self._artifact_exists(reference):
                    _fail("ARTIFACT_UNAVAILABLE", reference)
        return self._world._publish_record(contribution.bind(self.spec))


class ResearchWorld:
    """A long-lived PMW world independent of cohorts and agent processes."""

    def __init__(
        self,
        *,
        repo_path: Path,
        world_id: str | None,
        world_ref: str,
        broker: Any,
        writer: PmwWriterAuthority | None,
    ) -> None:
        self.repo_path = repo_path
        self.world_id = world_id
        self.world_ref = world_ref
        self._broker = broker
        self._writer = writer
        self._dispatcher: Any | None = None
        self._channel_token: str | None = None
        self._records_cache: OrderedDict[
            str, tuple[WorldAdmission, ...]
        ] = OrderedDict()
        self._cache_lock = threading.RLock()
        if writer is not None:
            assert PiAgentToolDispatcher is not None
            assert new_channel_token is not None
            self._channel_token = new_channel_token()
            self._dispatcher = PiAgentToolDispatcher(
                world=broker,
                binding=writer.build_binding(),
                channel_token=self._channel_token,
            )

    @classmethod
    def open(
        cls,
        repo_path: str | os.PathLike[str],
        *,
        world_id: str | None = None,
        world_ref: str = DEFAULT_WORLD_REF,
        writer: PmwWriterAuthority | None = None,
        required_snapshot_ref: str | None = None,
    ) -> "ResearchWorld":
        _require_core()
        raw_path = Path(repo_path)
        try:
            metadata = raw_path.lstat()
            selected = raw_path.resolve(strict=True)
        except OSError as exc:
            raise ResearchWorldError("WORLD_REPOSITORY_UNAVAILABLE") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            _fail("WORLD_REPOSITORY_UNSAFE")
        if (
            type(world_ref) is not str
            or _WORLD_REF.fullmatch(world_ref) is None
            or "//" in world_ref
            or "/../" in world_ref
        ):
            _fail("MALFORMED_WORLD_REF")
        if world_id is not None and (
            type(world_id) is not str or _WORLD_ID.fullmatch(world_id) is None
        ):
            _fail("MALFORMED_WORLD_ID")
        assert GitWorldBroker is not None
        try:
            broker = GitWorldBroker(selected, world_ref=world_ref)
            instance = cls(
                repo_path=selected,
                world_id=world_id,
                world_ref=world_ref,
                broker=broker,
                writer=writer,
            )
            if required_snapshot_ref is not None:
                required = _snapshot(
                    required_snapshot_ref, label="required_snapshot_ref"
                )
                instance.records(required)
        except ResearchWorldError:
            raise
        except AdmissionError as exc:
            code = getattr(exc, "code", "PMW_WORLD_OPEN_FAILED")
            raise ResearchWorldError("PMW_WORLD_OPEN_FAILED", str(code)) from exc
        return instance

    @property
    def writable(self) -> bool:
        return self._dispatcher is not None

    def head(self) -> str:
        try:
            value = self._broker.current_snapshot_ref()
        except AdmissionError as exc:
            raise ResearchWorldError(
                "PMW_WORLD_READ_FAILED", str(getattr(exc, "code", "UNKNOWN"))
            ) from exc
        return _snapshot(value, label="head")

    @staticmethod
    def _decode_row(row: object) -> WorldAdmission:
        if type(row) is not dict or set(row) != {
            "admission",
            "content_b64",
            "lineages",
        }:
            _fail("PMW_DERIVED_VIEW_INVALID", "row")
        admission = row.get("admission")
        encoded = row.get("content_b64")
        lineages = row.get("lineages")
        if type(admission) is not dict or type(encoded) is not str or type(lineages) is not list:
            _fail("PMW_DERIVED_VIEW_INVALID", "fields")
        admission_ref = _admission(
            admission.get("admission_ref"), label="admission_ref"
        )
        parent_values = admission.get("parent_refs")
        if type(parent_values) is not list:
            _fail("PMW_DERIVED_VIEW_INVALID", "parent_refs")
        parents = tuple(
            _admission(value, label="parent_ref") for value in parent_values
        )
        if len(parents) != len(set(parents)):
            _fail("PMW_DERIVED_VIEW_INVALID", "duplicate parent")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError) as exc:
            raise ResearchWorldError("PMW_DERIVED_VIEW_INVALID", "content") from exc
        return WorldAdmission(
            admission_ref=admission_ref,
            parent_refs=parents,
            content_bytes=content,
            _admission_bytes=canonical_json(admission),
            _lineages_bytes=canonical_json(lineages),
        )

    def records(self, snapshot_ref: str | None = None) -> tuple[WorldAdmission, ...]:
        selected = self.head() if snapshot_ref is None else _snapshot(
            snapshot_ref, label="snapshot_ref"
        )
        with self._cache_lock:
            cached = self._records_cache.get(selected)
            if cached is not None:
                self._records_cache.move_to_end(selected)
                return cached
        try:
            rows = self._broker.derive_view_rows(snapshot_ref=selected)
        except AdmissionError as exc:
            code = getattr(exc, "code", "UNKNOWN")
            if code == "SNAPSHOT_UNAVAILABLE":
                raise ResearchWorldError("SNAPSHOT_UNAVAILABLE") from exc
            raise ResearchWorldError("PMW_WORLD_READ_FAILED", str(code)) from exc
        decoded = tuple(self._decode_row(row) for row in rows)
        refs = tuple(row.admission_ref for row in decoded)
        if len(refs) != len(set(refs)):
            _fail("PMW_DERIVED_VIEW_INVALID", "duplicate admission")
        result = tuple(sorted(decoded, key=lambda row: row.admission_ref))
        with self._cache_lock:
            self._records_cache[selected] = result
            self._records_cache.move_to_end(selected)
            while len(self._records_cache) > 8:
                self._records_cache.popitem(last=False)
        return result

    def get(
        self,
        admission_ref: str,
        snapshot_ref: str | None = None,
    ) -> WorldAdmission:
        selected_ref = _admission(admission_ref, label="admission_ref")
        for row in self.records(snapshot_ref):
            if row.admission_ref == selected_ref:
                return row
        _fail("ADMISSION_UNAVAILABLE_AT_SNAPSHOT", selected_ref)

    def delta(
        self,
        since_snapshot_ref: str,
        snapshot_ref: str | None = None,
    ) -> tuple[WorldAdmission, ...]:
        since = _snapshot(since_snapshot_ref, label="since_snapshot_ref")
        current_ref = self.head() if snapshot_ref is None else _snapshot(
            snapshot_ref, label="snapshot_ref"
        )
        previous = {row.admission_ref for row in self.records(since)}
        current_rows = self.records(current_ref)
        current = {row.admission_ref for row in current_rows}
        if not previous <= current:
            _fail("SNAPSHOT_NOT_ANCESTOR")
        return tuple(row for row in current_rows if row.admission_ref not in previous)

    def bind_session(
        self,
        spec: "SessionSpec",
        *,
        artifact_exists: Callable[[str], bool] | None = None,
    ) -> BoundResearchSession:
        """Validate a frozen plan identity and return its only publish surface."""

        from ..sessions.model import SessionSpec

        if not isinstance(spec, SessionSpec):
            raise TypeError("spec must be SessionSpec")
        if self.world_id is None:
            _fail("WORLD_ID_UNBOUND")
        if spec.world_id != self.world_id or spec.world_ref != self.world_ref:
            _fail("SESSION_WORLD_MISMATCH")
        self.records(spec.base_snapshot_ref)
        return BoundResearchSession(
            self, spec, artifact_exists=artifact_exists
        )

    def _publish_record(self, record: ResearchRecord) -> PublishResult:
        """Trusted-host primitive; callers should use :meth:`bind_session`."""

        if not isinstance(record, ResearchRecord):
            _fail("MALFORMED_RESEARCH_RECORD")
        if self._dispatcher is None or self._writer is None or self._channel_token is None:
            _fail("WORLD_READ_ONLY")

        current_snapshot = self.head()
        current_rows = self.records(current_snapshot)
        current_refs = {row.admission_ref for row in current_rows}
        base_refs = {
            row.admission_ref for row in self.records(record.base_snapshot_ref)
        }
        if not base_refs <= current_refs:
            _fail("SNAPSHOT_NOT_ANCESTOR")
        missing = sorted(set(record.parent_refs) - current_refs)
        if missing:
            _fail("PARENT_UNAVAILABLE", missing[0])
        if len(record.to_bytes()) > self._writer.maximum_content_bytes:
            _fail("RECORD_SIZE_INVALID")

        call_id = f"research-publish-sha256-{record.content_sha256}"
        assert sign_tool_envelope is not None
        envelope = sign_tool_envelope(
            channel_token=self._channel_token,
            channel_ref=self._writer.channel_ref,
            tool_call_id=call_id,
            action=PMW_PROPOSE,
            arguments={
                "content": record.to_bytes().decode("utf-8"),
                "parent_refs": list(record.parent_refs),
            },
        )
        try:
            response = self._dispatcher.dispatch(envelope)
        except Exception as exc:
            code = getattr(exc, "code", "PMW_PUBLISH_FAILED")
            raise ResearchWorldError("PMW_PUBLISH_FAILED", str(code)) from exc
        result = response.get("result") if type(response) is dict else None
        admitted = result.get("admission") if type(result) is dict else None
        if (
            type(result) is not dict
            or result.get("status") != "ACCEPTED"
            or type(admitted) is not dict
        ):
            code = result.get("code") if type(result) is dict else "INVALID_RESPONSE"
            _fail("PMW_PUBLISH_REJECTED", str(code))
        admission_ref = _admission(
            admitted.get("admission_ref"), label="published admission_ref"
        )
        base_snapshot = _snapshot(
            result.get("base_snapshot_ref"), label="publish base_snapshot_ref"
        )
        resulting_snapshot = _snapshot(
            result.get("snapshot_ref"), label="publish snapshot_ref"
        )
        receipt_ref = result.get("receipt_ref")
        if type(receipt_ref) is not str or _RECEIPT_REF.fullmatch(receipt_ref) is None:
            _fail("PMW_PUBLISH_RESPONSE_INVALID", "receipt_ref")
        return PublishResult(
            admission_ref=admission_ref,
            base_snapshot_ref=base_snapshot,
            snapshot_ref=resulting_snapshot,
            receipt_ref=receipt_ref,
            content_sha256=record.content_sha256,
        )

    def audit(self) -> dict[str, object]:
        assert audit_git_world is not None
        try:
            value = audit_git_world(self.repo_path, world_ref=self.world_ref)
        except AdmissionError as exc:
            raise ResearchWorldError(
                "PMW_WORLD_AUDIT_FAILED", str(getattr(exc, "code", "UNKNOWN"))
            ) from exc
        if type(value) is not dict or value.get("valid") is not True:
            _fail("PMW_WORLD_AUDIT_FAILED")
        cloned = _json_clone(value)
        if type(cloned) is not dict:
            raise AssertionError("PMW audit is not an object")
        return cloned
