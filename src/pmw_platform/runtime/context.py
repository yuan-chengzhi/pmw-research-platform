"""Launch-bound model context-window selection.

This policy controls the active model window seen by a capable backend.  It is
not a cumulative token allowance and it does not claim to be a provider-route
capacity canary.  Backends must state how (or whether) they apply it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from types import MappingProxyType
from typing import Mapping, Sequence


CONTEXT_WINDOW_POLICY_SCHEMA = "PMW_RUNTIME_CONTEXT_WINDOW_POLICY_1"
CONTEXT_WINDOW_SEMANTICS = (
    "ACTIVE_MODEL_CONTEXT_WINDOW_TOKENS_NOT_CUMULATIVE_SESSION_USAGE"
)
MAXIMUM_CONTEXT_WINDOW_TOKENS = 2_147_483_647
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ContextWindowControl(str, Enum):
    """Backend implementation of a selected per-session model window."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    NATIVE_MODEL_WINDOW = "NATIVE_MODEL_WINDOW"


def _tokens(value: object, *, label: str) -> int:
    if (
        type(value) is not int
        or value <= 0
        or value > MAXIMUM_CONTEXT_WINDOW_TOKENS
    ):
        raise ValueError(
            f"{label} must be an integer between 1 and "
            f"{MAXIMUM_CONTEXT_WINDOW_TOKENS}"
        )
    return value


@dataclass(frozen=True, init=False)
class ContextWindowPolicy:
    """One default model window plus optional exact session overrides.

    ``None`` means that the backend's own declared model window remains in
    force.  The mathematical cohort plan stays free of execution treatment;
    this policy is instead serialized into the immutable runtime launch.
    """

    default_tokens: int | None
    _overrides: Mapping[str, int] = field(repr=False)

    def __init__(
        self,
        *,
        default_tokens: int | None = None,
        session_overrides: Mapping[str, int] | None = None,
    ) -> None:
        selected_default = (
            None
            if default_tokens is None
            else _tokens(default_tokens, label="default_tokens")
        )
        raw = {} if session_overrides is None else dict(session_overrides)
        normalized: dict[str, int] = {}
        for session_id, value in raw.items():
            if (
                type(session_id) is not str
                or _SESSION_ID.fullmatch(session_id) is None
            ):
                raise ValueError("session override has a malformed session ID")
            normalized[session_id] = _tokens(
                value, label=f"session override {session_id}"
            )
        object.__setattr__(self, "default_tokens", selected_default)
        object.__setattr__(
            self,
            "_overrides",
            MappingProxyType(dict(sorted(normalized.items()))),
        )

    @property
    def session_overrides(self) -> Mapping[str, int]:
        return self._overrides

    @property
    def configured(self) -> bool:
        return self.default_tokens is not None or bool(self._overrides)

    def for_session(self, session_id: str) -> int | None:
        return self._overrides.get(session_id, self.default_tokens)

    def bind(self, session_ids: Sequence[str]) -> dict[str, object]:
        """Return the exact launch value after rejecting stale overrides."""

        selected = tuple(session_ids)
        if (
            any(
                type(item) is not str or _SESSION_ID.fullmatch(item) is None
                for item in selected
            )
            or len(set(selected)) != len(selected)
        ):
            raise ValueError("session_ids must be canonical and unique")
        unknown = set(self._overrides).difference(selected)
        if unknown:
            raise ValueError(
                "context override does not name a session in the cohort: "
                + sorted(unknown)[0]
            )
        return {
            "schema": CONTEXT_WINDOW_POLICY_SCHEMA,
            "semantics": CONTEXT_WINDOW_SEMANTICS,
            "default_tokens": self.default_tokens,
            "session_overrides": [
                {
                    "session_id": session_id,
                    "context_window_tokens": value,
                }
                for session_id, value in self._overrides.items()
            ],
            "effective_sessions": [
                {
                    "session_id": session_id,
                    "context_window_tokens": self.for_session(session_id),
                }
                for session_id in selected
            ],
            "unset_semantics": "BACKEND_DECLARED_MODEL_WINDOW",
        }


__all__ = [
    "CONTEXT_WINDOW_POLICY_SCHEMA",
    "CONTEXT_WINDOW_SEMANTICS",
    "MAXIMUM_CONTEXT_WINDOW_TOKENS",
    "ContextWindowControl",
    "ContextWindowPolicy",
]
