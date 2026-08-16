from pathlib import Path

from pmw_platform.source_lock import load_core_lock


def test_installed_core_lock_binds_both_authorities_and_pyproject() -> None:
    lock = load_core_lock()
    pmw = lock.source("persistent-mathematical-worlds")
    frontier = lock.source("agent-math-frontier")

    assert len(lock.sha256) == 64
    assert pmw.commit == "4880f184c60bd34181302c5343ec0db95f154851"
    assert frontier.commit == "c737df34c84b7a3274b5be74c604aadfcc445478"
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert pmw.commit in pyproject
