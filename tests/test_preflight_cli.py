from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pmw_platform import cli


@pytest.mark.parametrize(("ready", "expected_exit"), [(True, 0), (False, 1)])
def test_session_preflight_reuses_launch_options_without_starting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ready: bool,
    expected_exit: int,
) -> None:
    cohort_id = "cohort-preflight"
    prepared = SimpleNamespace(plan=SimpleNamespace(cohort_id=cohort_id))
    backend = object()
    observed: dict[str, object] = {}
    emitted: list[object] = []
    report_value = {
        "schema": "PMW_RUNTIME_PREFLIGHT_REPORT_1",
        "ready": ready,
    }

    monkeypatch.setattr(
        cli,
        "authenticate_plan_bundle",
        lambda data_root, selected: (
            observed.update(auth=(data_root, selected)) or prepared
        ),
    )
    monkeypatch.setattr(cli, "_runtime_backend", lambda _args: backend)

    def fake_preflight(
        selected_prepared: object,
        selected_backend: object,
        *,
        limits: object,
        context_policy: object,
        publisher: object,
        checkers: object,
    ) -> object:
        observed["prepared"] = selected_prepared
        observed["backend"] = selected_backend
        observed["limits"] = limits
        observed["context_policy"] = context_policy
        observed["publisher"] = publisher
        observed["checkers"] = checkers
        return SimpleNamespace(
            ready=ready,
            to_value=lambda: report_value,
        )

    monkeypatch.setattr(cli, "preflight_prepared_cohort", fake_preflight)
    monkeypatch.setattr(
        cli,
        "run_prepared_cohort",
        lambda *_args, **_kwargs: pytest.fail("preflight started the runtime"),
    )
    monkeypatch.setattr(cli, "_emit", emitted.append)

    result = cli.main(
        [
            "--data-root",
            str(tmp_path),
            "session",
            "preflight",
            "--cohort",
            cohort_id,
            "--backend",
            "pi",
            "--backend-config",
            str(tmp_path / "pi.json"),
            "--startup-seconds",
            "15",
            "--no-wall-limit",
            "--stop-grace-seconds",
            "4",
            "--context-window-tokens",
            "400000",
            "--session-context-window",
            "cohort-preflight-s0002=360000",
        ]
    )

    assert result == expected_exit
    assert observed["auth"] == (tmp_path, cohort_id)
    assert observed["prepared"] is prepared
    assert observed["backend"] is backend
    assert observed["publisher"] is None
    assert [checker.name for checker in observed["checkers"]] == [
        "readiness-scope",
        "amf-apparatus",
    ]
    limits = observed["limits"]
    assert limits.startup_seconds == 15.0
    assert limits.session_wall_seconds is None
    assert limits.stop_grace_seconds == 4.0
    context = observed["context_policy"]
    assert context.default_tokens == 400_000
    assert context.session_overrides == {
        "cohort-preflight-s0002": 360_000,
    }
    assert emitted == [report_value]
