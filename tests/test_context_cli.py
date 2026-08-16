from __future__ import annotations

import pytest

from pmw_platform import cli


def test_start_parser_accepts_default_and_per_session_context_windows() -> None:
    args = cli.build_parser().parse_args(
        [
            "session",
            "start",
            "--cohort",
            "cohort-context",
            "--backend",
            "pi",
            "--backend-config",
            "/tmp/pi.json",
            "--context-window-tokens",
            "400000",
            "--session-context-window",
            "cohort-context-s0002=360000",
        ]
    )

    assert args.context_window_tokens == 400_000
    assert args.session_context_window == [
        ("cohort-context-s0002", 360_000)
    ]


@pytest.mark.parametrize(
    "value",
    ["s1", "../s1=400000", "s1=0", "s1=not-an-integer"],
)
def test_session_context_window_parser_rejects_malformed_values(
    value: str,
) -> None:
    with pytest.raises(cli.argparse.ArgumentTypeError):
        cli._session_context_window(value)
