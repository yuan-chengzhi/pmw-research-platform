from __future__ import annotations

import pytest

from pmw_platform.runtime.context import (
    CONTEXT_WINDOW_SEMANTICS,
    ContextWindowPolicy,
)


def test_unset_policy_preserves_backend_window() -> None:
    bound = ContextWindowPolicy().bind(["s1", "s2"])

    assert bound["semantics"] == CONTEXT_WINDOW_SEMANTICS
    assert bound["default_tokens"] is None
    assert bound["effective_sessions"] == [
        {"session_id": "s1", "context_window_tokens": None},
        {"session_id": "s2", "context_window_tokens": None},
    ]


def test_policy_defensively_copies_and_binds_exact_sessions() -> None:
    overrides = {"s2": 400_000}
    policy = ContextWindowPolicy(
        default_tokens=360_000,
        session_overrides=overrides,
    )
    overrides["s2"] = 1

    assert policy.for_session("s1") == 360_000
    assert policy.for_session("s2") == 400_000
    assert policy.bind(["s1", "s2"])["effective_sessions"] == [
        {"session_id": "s1", "context_window_tokens": 360_000},
        {"session_id": "s2", "context_window_tokens": 400_000},
    ]


def test_policy_rejects_stale_override_and_non_integer_tokens() -> None:
    with pytest.raises(ValueError, match="does not name a session"):
        ContextWindowPolicy(
            session_overrides={"missing": 400_000}
        ).bind(["s1"])

    with pytest.raises(ValueError, match="must be an integer"):
        ContextWindowPolicy(default_tokens=True)
