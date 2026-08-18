from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import os
from pathlib import Path

import pytest

from pmw_platform.runtime.context import ContextWindowPolicy
from pmw_platform.runtime.contracts import runtime_host_policy_value
from pmw_platform.runtime.resource_guard import (
    ResourceAccountingError,
    ResourceGuard,
    scan_tree,
)
from pmw_platform.runtime.safety import TreeLimits, load_named_profile
from pmw_platform.runtime.store import RuntimeStore
from pmw_platform.runtime.orchestrator import (
    not_configured_agenda_arm_launch_value,
)
from pmw_platform.verifier_kit import disabled_verifier_kit_launch_value
from pmw_platform.world.records import canonical_json


def _store(tmp_path: Path, session_ids: tuple[str, ...]) -> RuntimeStore:
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    store = RuntimeStore(cohort)
    backend = {
        "name": "resource-test",
        "protocol": "TEST_1",
        "public_config": {"implementation": "tests"},
    }
    publication = {
        "schema": "PMW_RUNTIME_PUBLICATION_IDENTITY_1",
        "mode": "DISABLED",
        "protocol": "NO_PUBLICATION_1",
        "public_config": {},
    }
    readiness = {
        "schema": "PMW_RUNTIME_REQUIRED_READINESS_1",
        "checks": [],
    }
    verifier_kit = disabled_verifier_kit_launch_value()
    agenda_arm = not_configured_agenda_arm_launch_value()
    launch = {
        "schema": "PMW_RUNTIME_LAUNCH_1",
        "created_at": "2026-08-16T00:00:00Z",
        "cohort_id": "resource-test",
        "world_id": "world-test",
        "world_ref": "refs/pmw/world-test",
        "base_snapshot_ref": f"snapshot/sha256/{'b' * 64}",
        "plan_sha256": "a" * 64,
        "briefing_sha256": "e" * 64,
        "safety_profile": "research-default",
        "safety_profile_sha256": "f" * 64,
        "core_lock_sha256": "c" * 64,
        "backend_sha256": hashlib.sha256(canonical_json(backend)).hexdigest(),
        "backend": backend,
        "publication_sha256": hashlib.sha256(
            canonical_json(publication)
        ).hexdigest(),
        "publication": publication,
        "concurrency": min(2, len(session_ids)),
        "session_ids": list(session_ids),
        "limits": {
            "startup_seconds": 60.0,
            "session_wall_seconds": 86400.0,
            "stop_grace_seconds": 10.0,
        },
        "context_window_policy": ContextWindowPolicy().bind(session_ids),
        "backend_context_window_control": "NOT_APPLICABLE",
        "required_readiness": readiness,
        "required_readiness_sha256": hashlib.sha256(
            canonical_json(readiness)
        ).hexdigest(),
        "verifier_kit": verifier_kit,
        "verifier_kit_sha256": hashlib.sha256(
            canonical_json(verifier_kit)
        ).hexdigest(),
        "agenda_arm": agenda_arm,
        "agenda_arm_sha256": hashlib.sha256(
            canonical_json(agenda_arm)
        ).hexdigest(),
        "host_policy": runtime_host_policy_value(),
    }
    store.create_launch(launch, session_ids=session_ids)
    return store


def _limits(*, maximum_total_bytes: int = 1024) -> TreeLimits:
    return TreeLimits(
        maximum_total_bytes=maximum_total_bytes,
        maximum_entries=100,
        maximum_file_bytes=None,
        maximum_depth=16,
        scan_mode="QUIESCENT",
        live_scan_interval_seconds=None,
    )


def test_tree_scan_counts_aggregate_bytes_without_hardlink_or_symlink_traps(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    original = root / "payload"
    original.write_bytes(b"0123456789")
    os.link(original, root / "second-name")
    outside = tmp_path / "outside"
    outside.write_bytes(b"x" * 500)
    (root / "outside-link").symlink_to(outside)

    snapshot = asyncio.run(
        scan_tree(root, _limits())
    )

    assert snapshot.total_bytes == 10
    assert snapshot.entries == 3
    assert snapshot.maximum_depth == 1


def test_quiescent_profile_has_only_first_and_terminal_tree_scans(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[object, dict[str, object], int, int]:
        store = _store(tmp_path, ("s-1",))
        profile = load_named_profile("research-default")
        profile = replace(profile, workspace=_limits(maximum_total_bytes=4))
        guard = ResourceGuard(profile, store, ("s-1",))
        await guard.start()
        initial = await guard.activate("s-1")
        active_after_initial = guard.active_task_count
        store.session_paths("s-1").workspace.joinpath("result").write_bytes(
            b"large"
        )
        event = await guard.finish("s-1")
        evidence = guard.evidence("s-1")
        await guard.close()
        return initial, evidence, active_after_initial, guard.active_task_count

    initial, evidence, active_after_initial, active_after_close = asyncio.run(
        scenario()
    )

    assert initial is None
    assert active_after_initial == 1  # only the host disk poller
    assert active_after_close == 0
    assert evidence["checks"] == {"disk": 2, "workspace": 2, "cache": 2}
    assert evidence["terminal_event"]["code"] == (  # type: ignore[index]
        "WORKSPACE_TOTAL_BYTES_EXCEEDED"
    )
    assert evidence["terminal_event"]["phase"] == "TERMINAL"  # type: ignore[index]


def test_guard_close_joins_live_monitor_tasks(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int]:
        store = _store(tmp_path, ("s-1",))
        profile = load_named_profile("research-default")
        live = TreeLimits(
            maximum_total_bytes=1024,
            maximum_entries=100,
            maximum_file_bytes=None,
            maximum_depth=16,
            scan_mode="LIVE_LATCHED",
            live_scan_interval_seconds=60.0,
        )
        profile = replace(profile, workspace=live, runtime_cache=live)
        guard = ResourceGuard(profile, store, ("s-1",))
        await guard.start()
        await guard.activate("s-1")
        before = guard.active_task_count
        await guard.close()
        return before, guard.active_task_count

    before, after = asyncio.run(scenario())
    assert before == 3
    assert after == 0


@pytest.mark.parametrize(
    "target,limits,build,expected_code",
    [
        (
            "workspace",
            TreeLimits(1024, 1, None, 16, "QUIESCENT", None),
            lambda root: (
                root.joinpath("one").write_bytes(b"1"),
                root.joinpath("two").write_bytes(b"2"),
            ),
            "WORKSPACE_ENTRY_LIMIT_EXCEEDED",
        ),
        (
            "workspace",
            TreeLimits(1024, 100, None, 1, "QUIESCENT", None),
            lambda root: (
                root.joinpath("nested").mkdir(),
                root.joinpath("nested", "deep").write_bytes(b"x"),
            ),
            "WORKSPACE_DEPTH_LIMIT_EXCEEDED",
        ),
        (
            "cache",
            TreeLimits(1024, 100, None, 1, "QUIESCENT", None),
            lambda root: (
                root.joinpath("nested").mkdir(),
                root.joinpath("nested", "deep").write_bytes(b"x"),
            ),
            "RUNTIME_CACHE_ENTRY_LIMIT_EXCEEDED",
        ),
    ],
)
def test_guard_enforces_entry_and_depth_aggregates(
    tmp_path: Path,
    target: str,
    limits: TreeLimits,
    build,
    expected_code: str,
) -> None:
    async def scenario():
        store = _store(tmp_path, ("s-1",))
        profile = load_named_profile("research-default")
        profile_field = "workspace" if target == "workspace" else "runtime_cache"
        profile = replace(profile, **{profile_field: limits})
        build(getattr(store.session_paths("s-1"), target))
        guard = ResourceGuard(profile, store, ("s-1",))
        await guard.start()
        event = await guard.activate("s-1")
        await guard.close()
        return event

    event = asyncio.run(scenario())
    assert event is not None
    assert event.code == expected_code
    assert event.target == target
    if target == "cache":
        assert event.observed == {"maximum_depth": 2}
        assert event.limits == {"maximum_depth": 1}


def test_single_file_cap_is_not_a_runtime_kill_rule(tmp_path: Path) -> None:
    async def scenario():
        store = _store(tmp_path, ("s-1",))
        profile = load_named_profile("research-default")
        workspace = TreeLimits(
            maximum_total_bytes=1024,
            maximum_entries=100,
            maximum_file_bytes=1,
            maximum_depth=16,
            scan_mode="QUIESCENT",
            live_scan_interval_seconds=None,
        )
        profile = replace(profile, workspace=workspace)
        store.session_paths("s-1").workspace.joinpath("large-file").write_bytes(
            b"ten bytes!"
        )
        guard = ResourceGuard(profile, store, ("s-1",))
        await guard.start()
        event = await guard.activate("s-1")
        await guard.finish("s-1")
        await guard.close()
        return event, guard.evidence("s-1")

    event, evidence = asyncio.run(scenario())
    assert event is None
    assert evidence["terminal_event"] is None


def test_tree_scan_never_follows_nested_directory_swap_to_symlink(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = tmp_path / "tree"
        nested = root / "nested"
        outside = tmp_path / "outside"
        nested.mkdir(parents=True)
        outside.mkdir()
        outside.joinpath("must-not-be-counted").write_bytes(b"outside")

        task = asyncio.create_task(
            scan_tree(root, _limits(), yield_every_entries=1)
        )
        await asyncio.sleep(0)
        nested.rename(tmp_path / "detached-nested")
        nested.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ResourceAccountingError):
            await task

    asyncio.run(scenario())


def test_tree_scan_keeps_root_fd_when_named_root_becomes_symlink(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = tmp_path / "tree"
        root.mkdir()
        root.joinpath("inside").write_bytes(b"inside")
        outside = tmp_path / "outside"
        outside.mkdir()
        outside.joinpath("must-not-be-counted").write_bytes(b"outside")

        task = asyncio.create_task(
            scan_tree(root, _limits(), yield_every_entries=1)
        )
        await asyncio.sleep(0)
        root.rename(tmp_path / "detached-root")
        root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ResourceAccountingError):
            await task

    asyncio.run(scenario())
