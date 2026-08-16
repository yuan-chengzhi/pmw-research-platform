from __future__ import annotations

import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from pmw_platform.config import ConfigError, WorldRegistration, WorldRegistry


SNAPSHOT = "snapshot/sha256/" + "a" * 64


def test_registry_is_sorted_atomic_and_creates_data_layout(tmp_path: Path) -> None:
    data = tmp_path / "data"
    second_repo = tmp_path / "second.git"
    first_repo = tmp_path / "first.git"
    second_repo.mkdir()
    first_repo.mkdir()
    registry = WorldRegistry(data)
    registry.add(WorldRegistration.create(
        name="zeta", repo=second_repo, world_ref="refs/pmw/world", seed_snapshot_ref=SNAPSHOT
    ))
    registry.add(WorldRegistration.create(
        name="alpha", repo=first_repo, world_ref="refs/pmw/world", seed_snapshot_ref=SNAPSHOT
    ))

    assert [row.name for row in registry.list()] == ["alpha", "zeta"]
    payload = json.loads((data / "registry.json").read_text())
    assert [row["name"] for row in payload["worlds"]] == ["alpha", "zeta"]
    assert all((data / name).is_dir() for name in (
        "worlds", "runs", "objects", "source-cache", "archive"
    ))


def test_registry_rejects_implicit_replace_and_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "world.git"
    repo.mkdir()
    row = WorldRegistration.create(
        name="math", repo=repo, world_ref="refs/pmw/world", seed_snapshot_ref=SNAPSHOT
    )
    registry = WorldRegistry(tmp_path / "data")
    registry.add(row)
    with pytest.raises(ConfigError, match="already registered"):
        registry.add(row)

    real = registry.path
    moved = real.with_name("registry.real.json")
    real.rename(moved)
    real.symlink_to(moved)
    with pytest.raises(ConfigError, match="symlink"):
        registry.list()


def test_registry_serializes_concurrent_adds(tmp_path: Path) -> None:
    data = tmp_path / "data"
    repos = [tmp_path / "one.git", tmp_path / "two.git"]
    for repo in repos:
        repo.mkdir()
    rows = [
        WorldRegistration.create(
            name=name,
            repo=repo,
            world_ref="refs/pmw/world",
            seed_snapshot_ref=SNAPSHOT,
        )
        for name, repo in zip(("one", "two"), repos, strict=True)
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(WorldRegistry(data).add, rows))

    assert [row.name for row in WorldRegistry(data).list()] == ["one", "two"]


def test_registry_rejects_duplicate_and_oversized_json(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    path = data / "registry.json"
    path.write_text('{"schema":"x","schema":"y","worlds":[]}')
    with pytest.raises(ConfigError, match="duplicate registry key"):
        WorldRegistry(data).list()

    path.write_bytes(b" " * 1_048_577)
    with pytest.raises(ConfigError, match="bounded regular file"):
        WorldRegistry(data).list()
