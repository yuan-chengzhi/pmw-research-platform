import json
from pathlib import Path

import pytest

from pmw_platform.source_lock import SourceLockError, load_core_lock


def test_installed_core_lock_binds_both_authorities_and_pyproject() -> None:
    lock = load_core_lock()
    pmw = lock.source("persistent-mathematical-worlds")
    frontier = lock.source("agent-math-frontier")

    assert len(lock.sha256) == 64
    assert pmw.commit == "4880f184c60bd34181302c5343ec0db95f154851"
    assert (
        pmw.materialized_tree_sha256
        == "88b6b9b9fce34df8e81cbc68bdbeb69f372281317da65f9505e08ec4d1f485b0"
    )
    assert frontier.commit == "c737df34c84b7a3274b5be74c604aadfcc445478"
    assert (
        frontier.materialized_tree_sha256
        == "cb4f1b7a77137c53993858330426941a704ddd1b77cc95c809f6b26231795894"
    )
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert pmw.commit in pyproject


def test_materialized_tree_digest_is_required(tmp_path: Path) -> None:
    installed = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "pmw_platform"
        / "locks"
        / "core-lock.json"
    )
    value = json.loads(installed.read_bytes())
    del value["dependencies"]["agent-math-frontier"][
        "materialized_tree_sha256"
    ]
    malformed = tmp_path / "core-lock.json"
    malformed.write_text(json.dumps(value, sort_keys=True))

    with pytest.raises(SourceLockError):
        load_core_lock(malformed)
