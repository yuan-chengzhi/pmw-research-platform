#!/usr/bin/python3
"""Deterministic command-backend worker for zero-model runtime acceptance.

This executable proves the generic launch, environment, concurrency and
settlement path without contacting a model or publishing to the PMW world.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _read_object(environment_name: str) -> dict[str, object]:
    path = Path(os.environ[environment_name])
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError(f"{environment_name} is not a JSON object")
    return value


def main() -> None:
    briefing = _read_object("PMW_BRIEFING_PATH")
    invocation = _read_object("PMW_INVOCATION_PATH")
    session_id = os.environ["PMW_SESSION_ID"]
    session = invocation.get("session")
    if type(session) is not dict or session.get("session_id") != session_id:
        raise ValueError("invocation/session environment mismatch")
    outcome = {
        "schema": "PMW_RUNTIME_BACKEND_OUTCOME_1",
        "success": True,
        "terminal_reason": "MODEL_FREE_ACCEPTANCE_COMPLETED",
        "summary": "Deterministic runtime acceptance completed; no model was called.",
        "usage": {"model_calls": 0, "network_calls": 0},
        "evidence": {
            "briefing_schema": briefing.get("schema"),
            "invocation_schema": invocation.get("schema"),
            "session_id": session_id,
        },
        "contributions": [],
    }
    raw = json.dumps(
        outcome,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    Path(os.environ["PMW_RESULT_PATH"]).write_bytes(raw)


if __name__ == "__main__":
    main()
