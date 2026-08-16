"""Canonical, bounded records authored by research sessions.

The PMW admission is the durable identity of a record.  This module defines
only the small JSON content placed inside that admission; timestamps and
runtime receipts deliberately live outside mathematical state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Mapping, NoReturn, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from ..sessions.model import SessionSpec


RESEARCH_RECORD_SCHEMA = "PMW_RESEARCH_RECORD_1"
RESEARCH_KINDS = frozenset({
    "NOTE",
    "NEED",
    "ATTEMPT",
    "RESULT",
    "OBJECTION",
    "CHECKPOINT",
})
MAXIMUM_RECORD_BYTES = 65_536
MAXIMUM_BODY_BYTES = 48_000
MAXIMUM_TITLE_BYTES = 1_024
MAXIMUM_PAYLOAD_NODES = 4_096
MAXIMUM_PAYLOAD_DEPTH = 32
MAXIMUM_COLLECTION_ITEMS = 1_024
MAXIMUM_STRING_BYTES = 65_536
MAXIMUM_INTEGER = (1 << 63) - 1

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROBLEM_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SNAPSHOT_REF = re.compile(r"^snapshot/sha256/[0-9a-f]{64}$")
_ADMISSION_REF = re.compile(r"^admission/sha256/[0-9a-f]{64}$")
_ARTIFACT_REF = re.compile(r"^artifact/sha256/[0-9a-f]{64}$")
_FIELDS = frozenset({
    "schema",
    "world_id",
    "cohort_id",
    "session_id",
    "base_snapshot_ref",
    "kind",
    "problem_ids",
    "parent_refs",
    "title",
    "body",
    "artifact_refs",
    "payload",
})


class ResearchRecordError(ValueError):
    """Stable validation error for one proposed mathematical record."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _fail(code: str, detail: str = "") -> NoReturn:
    raise ResearchRecordError(code, detail)


def canonical_json(value: object) -> bytes:
    """Encode the platform's deterministic JSON subset."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ResearchRecordError("MALFORMED_JSON") from exc


def _duplicate_rejecting_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _parse_integer(encoded: str) -> int:
    value = int(encoded)
    if not -MAXIMUM_INTEGER <= value <= MAXIMUM_INTEGER:
        raise ValueError("integer out of bounds")
    return value


def _reject_number(_encoded: str) -> NoReturn:
    raise ValueError("floating-point JSON is unsupported")


def strict_json(raw: bytes) -> object:
    """Parse canonical JSON, rejecting duplicate keys and non-integer numbers."""

    if type(raw) is not bytes or not raw or len(raw) > MAXIMUM_RECORD_BYTES:
        _fail("RECORD_SIZE_INVALID")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_int=_parse_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ResearchRecordError("MALFORMED_JSON") from exc
    if canonical_json(value) != raw:
        _fail("NONCANONICAL_JSON")
    return value


def _text(
    value: object,
    *,
    label: str,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or (not allow_empty and not value):
        _fail("MALFORMED_FIELD", label)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ResearchRecordError("MALFORMED_FIELD", label) from exc
    if len(encoded) > maximum_bytes:
        _fail("FIELD_LIMIT_EXCEEDED", label)
    return value


def _identifier(value: object, *, label: str, pattern: re.Pattern[str]) -> str:
    selected = _text(value, label=label, maximum_bytes=128)
    if pattern.fullmatch(selected) is None:
        _fail("MALFORMED_FIELD", label)
    return selected


def _reference(
    value: object,
    *,
    label: str,
    pattern: re.Pattern[str],
) -> str:
    selected = _text(value, label=label, maximum_bytes=96)
    if pattern.fullmatch(selected) is None:
        _fail("MALFORMED_FIELD", label)
    return selected


def _sorted_unique(
    values: Sequence[object],
    *,
    label: str,
    validator: Any,
    maximum: int,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail("MALFORMED_FIELD", label)
    if len(values) > maximum:
        _fail("FIELD_LIMIT_EXCEEDED", label)
    selected = tuple(validator(value) for value in values)
    if len(selected) != len(set(selected)):
        _fail("MALFORMED_FIELD", f"{label}: duplicate")
    return tuple(sorted(selected))


def _validate_payload(value: object) -> bytes:
    remaining = MAXIMUM_PAYLOAD_NODES
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        remaining -= 1
        if remaining < 0 or depth > MAXIMUM_PAYLOAD_DEPTH:
            _fail("PAYLOAD_LIMIT_EXCEEDED")
        if type(item) is dict:
            if len(item) > MAXIMUM_COLLECTION_ITEMS:
                _fail("PAYLOAD_LIMIT_EXCEEDED")
            for key, child in item.items():
                _text(key, label="payload key", maximum_bytes=256)
                stack.append((child, depth + 1))
        elif type(item) is list:
            if len(item) > MAXIMUM_COLLECTION_ITEMS:
                _fail("PAYLOAD_LIMIT_EXCEEDED")
            stack.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            _text(
                item,
                label="payload string",
                maximum_bytes=MAXIMUM_STRING_BYTES,
                allow_empty=True,
            )
        elif type(item) is int:
            if not -MAXIMUM_INTEGER <= item <= MAXIMUM_INTEGER:
                _fail("PAYLOAD_INTEGER_OUT_OF_BOUNDS")
        elif item is not None and type(item) is not bool:
            _fail("PAYLOAD_TYPE_UNSUPPORTED")
    return canonical_json(value)


@dataclass(frozen=True, init=False)
class ResearchContribution:
    """Untrusted mathematical content without launch identity fields.

    A research process may propose this value. Only the trusted host may bind
    it to a :class:`SessionSpec` and turn it into a durable
    :class:`ResearchRecord`.
    """

    kind: str
    problem_ids: tuple[str, ...]
    parent_refs: tuple[str, ...]
    title: str
    body: str
    artifact_refs: tuple[str, ...]
    _payload_bytes: bytes = field(repr=False)

    def __init__(
        self,
        *,
        kind: str,
        title: str,
        body: str,
        problem_ids: Sequence[str] = (),
        parent_refs: Sequence[str] = (),
        artifact_refs: Sequence[str] = (),
        payload: Mapping[str, object] | None = None,
    ) -> None:
        if type(kind) is not str or kind not in RESEARCH_KINDS:
            _fail("MALFORMED_FIELD", "kind")
        selected_problems = _sorted_unique(
            problem_ids,
            label="problem_ids",
            validator=lambda value: _identifier(
                value, label="problem_id", pattern=_PROBLEM_ID
            ),
            maximum=64,
        )
        selected_parents = _sorted_unique(
            parent_refs,
            label="parent_refs",
            validator=lambda value: _reference(
                value, label="parent_ref", pattern=_ADMISSION_REF
            ),
            maximum=16,
        )
        selected_artifacts = _sorted_unique(
            artifact_refs,
            label="artifact_refs",
            validator=lambda value: _reference(
                value, label="artifact_ref", pattern=_ARTIFACT_REF
            ),
            maximum=64,
        )
        selected_title = _text(
            title, label="title", maximum_bytes=MAXIMUM_TITLE_BYTES
        )
        selected_body = _text(
            body, label="body", maximum_bytes=MAXIMUM_BODY_BYTES
        )
        selected_payload: object = {} if payload is None else payload
        if type(selected_payload) is not dict:
            _fail("MALFORMED_FIELD", "payload")
        payload_bytes = _validate_payload(selected_payload)
        for name, value in (
            ("kind", kind),
            ("problem_ids", selected_problems),
            ("parent_refs", selected_parents),
            ("title", selected_title),
            ("body", selected_body),
            ("artifact_refs", selected_artifacts),
            ("_payload_bytes", payload_bytes),
        ):
            object.__setattr__(self, name, value)

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self._payload_bytes.decode("utf-8"))
        if type(value) is not dict:
            raise AssertionError("research contribution payload is not an object")
        return value

    def bind(self, spec: "SessionSpec") -> "ResearchRecord":
        """Let the host inject the complete immutable session identity."""

        from ..sessions.model import SessionSpec

        if not isinstance(spec, SessionSpec):
            raise TypeError("spec must be SessionSpec")
        return ResearchRecord(
            world_id=spec.world_id,
            cohort_id=spec.cohort_id,
            session_id=spec.session_id,
            base_snapshot_ref=spec.base_snapshot_ref,
            kind=self.kind,
            problem_ids=self.problem_ids,
            parent_refs=self.parent_refs,
            title=self.title,
            body=self.body,
            artifact_refs=self.artifact_refs,
            payload=self.payload,
        )


@dataclass(frozen=True, init=False)
class ResearchRecord:
    """Immutable canonical content for one session-authored PMW admission."""

    world_id: str
    cohort_id: str
    session_id: str
    base_snapshot_ref: str
    kind: str
    problem_ids: tuple[str, ...]
    parent_refs: tuple[str, ...]
    title: str
    body: str
    artifact_refs: tuple[str, ...]
    _payload_bytes: bytes = field(repr=False)

    def __init__(
        self,
        *,
        world_id: str,
        cohort_id: str,
        session_id: str,
        base_snapshot_ref: str,
        kind: str,
        title: str,
        body: str,
        problem_ids: Sequence[str] = (),
        parent_refs: Sequence[str] = (),
        artifact_refs: Sequence[str] = (),
        payload: Mapping[str, object] | None = None,
    ) -> None:
        selected_world = _identifier(world_id, label="world_id", pattern=_ID)
        selected_cohort = _identifier(cohort_id, label="cohort_id", pattern=_ID)
        selected_session = _identifier(session_id, label="session_id", pattern=_ID)
        selected_snapshot = _reference(
            base_snapshot_ref,
            label="base_snapshot_ref",
            pattern=_SNAPSHOT_REF,
        )
        if type(kind) is not str or kind not in RESEARCH_KINDS:
            _fail("MALFORMED_FIELD", "kind")
        selected_problems = _sorted_unique(
            problem_ids,
            label="problem_ids",
            validator=lambda value: _identifier(
                value, label="problem_id", pattern=_PROBLEM_ID
            ),
            maximum=64,
        )
        selected_parents = _sorted_unique(
            parent_refs,
            label="parent_refs",
            validator=lambda value: _reference(
                value, label="parent_ref", pattern=_ADMISSION_REF
            ),
            maximum=16,
        )
        selected_artifacts = _sorted_unique(
            artifact_refs,
            label="artifact_refs",
            validator=lambda value: _reference(
                value, label="artifact_ref", pattern=_ARTIFACT_REF
            ),
            maximum=64,
        )
        selected_title = _text(
            title, label="title", maximum_bytes=MAXIMUM_TITLE_BYTES
        )
        selected_body = _text(
            body, label="body", maximum_bytes=MAXIMUM_BODY_BYTES
        )
        selected_payload: object = {} if payload is None else payload
        if type(selected_payload) is not dict:
            _fail("MALFORMED_FIELD", "payload")
        payload_bytes = _validate_payload(selected_payload)

        for name, value in (
            ("world_id", selected_world),
            ("cohort_id", selected_cohort),
            ("session_id", selected_session),
            ("base_snapshot_ref", selected_snapshot),
            ("kind", kind),
            ("problem_ids", selected_problems),
            ("parent_refs", selected_parents),
            ("title", selected_title),
            ("body", selected_body),
            ("artifact_refs", selected_artifacts),
            ("_payload_bytes", payload_bytes),
        ):
            object.__setattr__(self, name, value)
        if len(self.to_bytes()) > MAXIMUM_RECORD_BYTES:
            _fail("RECORD_SIZE_INVALID")

    @property
    def payload(self) -> dict[str, object]:
        """Return a detached payload value; callers cannot mutate the record."""

        value = json.loads(self._payload_bytes.decode("utf-8"))
        if type(value) is not dict:  # constructor invariant
            raise AssertionError("research payload is not an object")
        return value

    def to_value(self) -> dict[str, object]:
        return {
            "schema": RESEARCH_RECORD_SCHEMA,
            "world_id": self.world_id,
            "cohort_id": self.cohort_id,
            "session_id": self.session_id,
            "base_snapshot_ref": self.base_snapshot_ref,
            "kind": self.kind,
            "problem_ids": list(self.problem_ids),
            "parent_refs": list(self.parent_refs),
            "title": self.title,
            "body": self.body,
            "artifact_refs": list(self.artifact_refs),
            "payload": self.payload,
        }

    def to_bytes(self) -> bytes:
        return canonical_json(self.to_value())

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_value(cls, value: object) -> "ResearchRecord":
        if type(value) is not dict or set(value) != _FIELDS:
            _fail("MALFORMED_RECORD", "fields")
        if value.get("schema") != RESEARCH_RECORD_SCHEMA:
            _fail("MALFORMED_RECORD", "schema")
        for label in ("problem_ids", "parent_refs", "artifact_refs"):
            if type(value.get(label)) is not list:
                _fail("MALFORMED_RECORD", label)
        payload = value.get("payload")
        if type(payload) is not dict:
            _fail("MALFORMED_RECORD", "payload")
        return cls(
            world_id=value.get("world_id"),  # type: ignore[arg-type]
            cohort_id=value.get("cohort_id"),  # type: ignore[arg-type]
            session_id=value.get("session_id"),  # type: ignore[arg-type]
            base_snapshot_ref=value.get("base_snapshot_ref"),  # type: ignore[arg-type]
            kind=value.get("kind"),  # type: ignore[arg-type]
            title=value.get("title"),  # type: ignore[arg-type]
            body=value.get("body"),  # type: ignore[arg-type]
            problem_ids=value["problem_ids"],  # type: ignore[arg-type]
            parent_refs=value["parent_refs"],  # type: ignore[arg-type]
            artifact_refs=value["artifact_refs"],  # type: ignore[arg-type]
            payload=payload,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ResearchRecord":
        record = cls.from_value(strict_json(raw))
        if record.to_bytes() != raw:
            _fail("NONCANONICAL_RECORD")
        return record
