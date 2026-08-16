"""Immutable session and cohort manifests.

``count`` is intentionally a construction convenience only.  Once a plan is
created, its explicit :class:`SessionSpec` entries are the authority for which
sessions exist.  This keeps replay independent of CLI defaults or later
configuration changes.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, ClassVar, Mapping


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SNAPSHOT = re.compile(r"^snapshot/sha256/[0-9a-f]{64}$")
_WORLD_REF = re.compile(r"^refs/pmw/[A-Za-z0-9][A-Za-z0-9._/-]{0,240}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAXIMUM_SESSIONS = 4_096


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{field} must match {_IDENTIFIER.pattern!r}; got {value!r}"
        )
    return value


def _snapshot(value: object) -> str:
    if not isinstance(value, str) or _SNAPSHOT.fullmatch(value) is None:
        raise ValueError("base_snapshot_ref must be a canonical snapshot/sha256 ref")
    return value


def _world_ref(value: object) -> str:
    if (
        not isinstance(value, str)
        or _WORLD_REF.fullmatch(value) is None
        or "//" in value
        or "/../" in value
    ):
        raise ValueError("world_ref must be a canonical refs/pmw/... name")
    return value


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """The complete immutable launch identity for one research session."""

    session_id: str
    cohort_id: str
    world_id: str
    world_ref: str
    base_snapshot_ref: str
    safety_profile: str
    safety_profile_sha256: str
    core_lock_sha256: str
    briefing_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.session_id, field="session_id")
        _identifier(self.cohort_id, field="cohort_id")
        _identifier(self.world_id, field="world_id")
        _world_ref(self.world_ref)
        _snapshot(self.base_snapshot_ref)
        _identifier(self.safety_profile, field="safety_profile")
        _sha256(self.safety_profile_sha256, field="safety_profile_sha256")
        _sha256(self.core_lock_sha256, field="core_lock_sha256")
        _sha256(self.briefing_sha256, field="briefing_sha256")

    def to_manifest(self) -> dict[str, str]:
        """Return only the varying field; common identity lives once in plan."""

        return {"session_id": self.session_id}

    @classmethod
    def from_manifest(
        cls,
        value: Mapping[str, object],
        *,
        cohort_id: str,
        world_id: str,
        world_ref: str,
        base_snapshot_ref: str,
        safety_profile: str,
        safety_profile_sha256: str,
        core_lock_sha256: str,
        briefing_sha256: str,
    ) -> SessionSpec:
        expected = {"session_id"}
        if set(value) != expected:
            raise ValueError(
                "session manifest fields must be exactly "
                f"{sorted(expected)!r}; got {sorted(value)!r}"
            )
        return cls(
            session_id=_identifier(value["session_id"], field="session_id"),
            cohort_id=cohort_id,
            world_id=world_id,
            world_ref=world_ref,
            base_snapshot_ref=base_snapshot_ref,
            safety_profile=safety_profile,
            safety_profile_sha256=safety_profile_sha256,
            core_lock_sha256=core_lock_sha256,
            briefing_sha256=briefing_sha256,
        )


@dataclass(frozen=True, slots=True)
class CohortPlan:
    """An explicit, replayable set of sessions with one concurrency bound."""

    SCHEMA: ClassVar[str] = "PMW_COHORT_PLAN_1"

    cohort_id: str
    world_id: str
    world_ref: str
    base_snapshot_ref: str
    safety_profile: str
    safety_profile_sha256: str
    core_lock_sha256: str
    briefing_sha256: str
    concurrency: int
    sessions: tuple[SessionSpec, ...]

    def __post_init__(self) -> None:
        _identifier(self.cohort_id, field="cohort_id")
        _identifier(self.world_id, field="world_id")
        _world_ref(self.world_ref)
        _snapshot(self.base_snapshot_ref)
        _identifier(self.safety_profile, field="safety_profile")
        _sha256(self.safety_profile_sha256, field="safety_profile_sha256")
        _sha256(self.core_lock_sha256, field="core_lock_sha256")
        _sha256(self.briefing_sha256, field="briefing_sha256")
        if isinstance(self.concurrency, bool) or not isinstance(
            self.concurrency, int
        ):
            raise ValueError("concurrency must be an integer")
        if not isinstance(self.sessions, tuple):
            raise ValueError("sessions must be an immutable tuple")
        if not self.sessions:
            raise ValueError("a cohort must contain at least one session")
        if len(self.sessions) > MAXIMUM_SESSIONS:
            raise ValueError(f"session count exceeds host capacity {MAXIMUM_SESSIONS}")
        if not 1 <= self.concurrency <= len(self.sessions):
            raise ValueError("concurrency must be between 1 and session count")

        ids: set[str] = set()
        for session in self.sessions:
            if session.cohort_id != self.cohort_id:
                raise ValueError(
                    f"session {session.session_id!r} belongs to a different cohort"
                )
            if session.world_id != self.world_id:
                raise ValueError(
                    f"session {session.session_id!r} belongs to a different world"
                )
            if session.world_ref != self.world_ref:
                raise ValueError(
                    f"session {session.session_id!r} uses a different world ref"
                )
            if session.base_snapshot_ref != self.base_snapshot_ref:
                raise ValueError(
                    f"session {session.session_id!r} uses a different base snapshot"
                )
            if session.safety_profile != self.safety_profile:
                raise ValueError(
                    f"session {session.session_id!r} uses a different safety profile"
                )
            for field in (
                "safety_profile_sha256",
                "core_lock_sha256",
                "briefing_sha256",
            ):
                if getattr(session, field) != getattr(self, field):
                    raise ValueError(
                        f"session {session.session_id!r} uses a different {field}"
                    )
            if session.session_id in ids:
                raise ValueError(f"duplicate session_id: {session.session_id!r}")
            ids.add(session.session_id)

    @property
    def count(self) -> int:
        """Return the number of frozen specs; it is not stored separately."""

        return len(self.sessions)

    @classmethod
    def generate(
        cls,
        *,
        cohort_id: str,
        world_id: str,
        world_ref: str,
        base_snapshot_ref: str,
        safety_profile: str,
        safety_profile_sha256: str,
        core_lock_sha256: str,
        briefing_sha256: str,
        count: int,
        concurrency: int,
    ) -> CohortPlan:
        """Generate stable IDs, then freeze them as explicit session specs."""

        cohort_id = _identifier(cohort_id, field="cohort_id")
        world_id = _identifier(world_id, field="world_id")
        world_ref = _world_ref(world_ref)
        base_snapshot_ref = _snapshot(base_snapshot_ref)
        safety_profile = _identifier(safety_profile, field="safety_profile")
        safety_profile_sha256 = _sha256(
            safety_profile_sha256, field="safety_profile_sha256"
        )
        core_lock_sha256 = _sha256(core_lock_sha256, field="core_lock_sha256")
        briefing_sha256 = _sha256(briefing_sha256, field="briefing_sha256")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        if count > MAXIMUM_SESSIONS:
            raise ValueError(f"count exceeds host capacity {MAXIMUM_SESSIONS}")
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or not 1 <= concurrency <= count
        ):
            raise ValueError("concurrency must be between 1 and count")

        sessions = tuple(
            SessionSpec(
                # Fixed width keeps an ordinal's ID independent of how many
                # peers were requested in the same construction call.
                session_id=f"{cohort_id}-session-{ordinal:04d}",
                cohort_id=cohort_id,
                world_id=world_id,
                world_ref=world_ref,
                base_snapshot_ref=base_snapshot_ref,
                safety_profile=safety_profile,
                safety_profile_sha256=safety_profile_sha256,
                core_lock_sha256=core_lock_sha256,
                briefing_sha256=briefing_sha256,
            )
            for ordinal in range(1, count + 1)
        )
        return cls(
            cohort_id=cohort_id,
            world_id=world_id,
            world_ref=world_ref,
            base_snapshot_ref=base_snapshot_ref,
            safety_profile=safety_profile,
            safety_profile_sha256=safety_profile_sha256,
            core_lock_sha256=core_lock_sha256,
            briefing_sha256=briefing_sha256,
            concurrency=concurrency,
            sessions=sessions,
        )

    def to_manifest(self) -> dict[str, Any]:
        """Return a JSON-compatible manifest containing every exact ID."""

        return {
            "schema": self.SCHEMA,
            "cohort_id": self.cohort_id,
            "world_id": self.world_id,
            "world_ref": self.world_ref,
            "base_snapshot_ref": self.base_snapshot_ref,
            "safety_profile": self.safety_profile,
            "safety_profile_sha256": self.safety_profile_sha256,
            "core_lock_sha256": self.core_lock_sha256,
            "briefing_sha256": self.briefing_sha256,
            "concurrency": self.concurrency,
            "sessions": [session.to_manifest() for session in self.sessions],
        }

    def to_bytes(self) -> bytes:
        """Return the exact canonical bytes persisted as ``plan.json``."""

        return (
            json.dumps(
                self.to_manifest(),
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_manifest(cls, value: Mapping[str, object]) -> CohortPlan:
        expected = {
            "schema",
            "cohort_id",
            "world_id",
            "world_ref",
            "base_snapshot_ref",
            "safety_profile",
            "safety_profile_sha256",
            "core_lock_sha256",
            "briefing_sha256",
            "concurrency",
            "sessions",
        }
        if set(value) != expected:
            raise ValueError(
                "cohort manifest fields must be exactly "
                f"{sorted(expected)!r}; got {sorted(value)!r}"
            )
        if value["schema"] != cls.SCHEMA:
            raise ValueError(f"unsupported cohort schema: {value['schema']!r}")

        cohort_id = _identifier(value["cohort_id"], field="cohort_id")
        world_id = _identifier(value["world_id"], field="world_id")
        world_ref = _world_ref(value["world_ref"])
        base_snapshot_ref = _snapshot(value["base_snapshot_ref"])
        safety_profile = _identifier(
            value["safety_profile"], field="safety_profile"
        )
        safety_profile_sha256 = _sha256(
            value["safety_profile_sha256"], field="safety_profile_sha256"
        )
        core_lock_sha256 = _sha256(
            value["core_lock_sha256"], field="core_lock_sha256"
        )
        briefing_sha256 = _sha256(
            value["briefing_sha256"], field="briefing_sha256"
        )

        raw_sessions = value["sessions"]
        if not isinstance(raw_sessions, list):
            raise ValueError("sessions must be a list")
        if not 1 <= len(raw_sessions) <= MAXIMUM_SESSIONS:
            raise ValueError(
                f"session count must be between 1 and {MAXIMUM_SESSIONS}"
            )
        sessions: list[SessionSpec] = []
        for raw in raw_sessions:
            if not isinstance(raw, Mapping):
                raise ValueError("each session must be a mapping")
            sessions.append(
                SessionSpec.from_manifest(
                    raw,
                    cohort_id=cohort_id,
                    world_id=world_id,
                    world_ref=world_ref,
                    base_snapshot_ref=base_snapshot_ref,
                    safety_profile=safety_profile,
                    safety_profile_sha256=safety_profile_sha256,
                    core_lock_sha256=core_lock_sha256,
                    briefing_sha256=briefing_sha256,
                )
            )

        concurrency = value["concurrency"]
        if isinstance(concurrency, bool) or not isinstance(concurrency, int):
            raise ValueError("concurrency must be an integer")
        return cls(
            cohort_id=cohort_id,
            world_id=world_id,
            world_ref=world_ref,
            base_snapshot_ref=base_snapshot_ref,
            safety_profile=safety_profile,
            safety_profile_sha256=safety_profile_sha256,
            core_lock_sha256=core_lock_sha256,
            briefing_sha256=briefing_sha256,
            concurrency=concurrency,
            sessions=tuple(sessions),
        )
