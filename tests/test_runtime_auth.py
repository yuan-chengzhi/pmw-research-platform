from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from pmw_platform.runtime import auth
from pmw_platform.runtime.auth import RuntimeAuthenticationError
from pmw_platform.sessions import CohortPlan


SNAPSHOT = "snapshot/sha256/" + "a" * 64
ARTIFACT_REF = "artifact/sha256/" + "b" * 64


class _FakeWorld:
    def __init__(self) -> None:
        self.delta_calls: list[tuple[str, str]] = []

    def head(self) -> str:
        return SNAPSHOT

    def delta(self, base: str, head: str) -> tuple[()]:
        self.delta_calls.append((base, head))
        return ()


def _install_valid_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, CohortPlan, dict[str, object]]:
    data_root = tmp_path / "data"
    cohort_root = data_root / "runs" / "cohort-auth"
    cohort_root.mkdir(parents=True)
    briefing = (
        '{"nested":["' + ARTIFACT_REF + '","' + ARTIFACT_REF + '"]}\n'
    ).encode()
    plan = CohortPlan.generate(
        cohort_id="cohort-auth",
        world_id="math-frontier",
        world_ref="refs/pmw/math-frontier",
        base_snapshot_ref=SNAPSHOT,
        safety_profile="research-default",
        safety_profile_sha256="c" * 64,
        core_lock_sha256="d" * 64,
        briefing_sha256=hashlib.sha256(briefing).hexdigest(),
        count=2,
        concurrency=1,
    )
    (cohort_root / "plan.json").write_bytes(plan.to_bytes())
    (cohort_root / "briefing.json").write_bytes(briefing)

    state: dict[str, object] = {
        "profile_sha": plan.safety_profile_sha256,
        "core_sha": plan.core_lock_sha256,
        "registration": SimpleNamespace(
            name=plan.world_id,
            repo=str(tmp_path / "world.git"),
            world_ref=plan.world_ref,
        ),
        "missing": (),
        "audited": None,
        "rebuilt_briefing": briefing,
        "source_checks": 0,
    }
    fake_world = _FakeWorld()

    monkeypatch.setattr(auth, "load_plan", lambda _path: plan)
    monkeypatch.setattr(auth, "load_briefing", lambda _path: briefing)
    monkeypatch.setattr(
        auth,
        "load_named_profile",
        lambda *_args, **_kwargs: SimpleNamespace(sha256=state["profile_sha"]),
    )
    monkeypatch.setattr(
        auth,
        "load_core_lock",
        lambda *_args, **_kwargs: SimpleNamespace(sha256=state["core_sha"]),
    )

    def validate_source(_lock: object) -> None:
        state["source_checks"] = int(state["source_checks"]) + 1

    monkeypatch.setattr(auth, "_validate_loaded_pmw_source", validate_source)

    class Registry:
        def __init__(self, root: Path) -> None:
            assert root == data_root

        def get(self, _world_id: str) -> object:
            return state["registration"]

    class Worlds:
        @staticmethod
        def open(*_args: object, **_kwargs: object) -> _FakeWorld:
            return fake_world

    class Store:
        def __init__(self, root: Path) -> None:
            assert root == data_root

        def audit_refs(self, refs: tuple[str, ...]) -> object:
            state["audited"] = refs
            return state["missing"]

        def exists(self, reference: str) -> bool:
            return reference not in state["missing"]

    monkeypatch.setattr(auth, "WorldRegistry", Registry)
    monkeypatch.setattr(auth, "ResearchWorld", Worlds)
    monkeypatch.setattr(auth, "ArtifactStore", Store)
    monkeypatch.setattr(
        auth,
        "build_mathematical_situation",
        lambda *_args, **_kwargs: SimpleNamespace(
            bytes=state["rebuilt_briefing"],
            sha256=hashlib.sha256(state["rebuilt_briefing"]).hexdigest(),
        ),
    )
    state["world"] = fake_world
    state["briefing"] = briefing
    return data_root, plan, state


def test_authenticate_plan_bundle_closes_all_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, plan, state = _install_valid_bundle(tmp_path, monkeypatch)

    prepared = auth.authenticate_plan_bundle(data_root, plan.cohort_id)

    assert prepared.plan is plan
    assert prepared.plan_sha256 == plan.sha256
    assert prepared.briefing_bytes == state["briefing"]
    assert prepared.plan_path == data_root / "runs" / plan.cohort_id / "plan.json"
    assert state["audited"] == (ARTIFACT_REF,)
    assert state["source_checks"] == 1
    assert state["world"].delta_calls == [(SNAPSHOT, SNAPSHOT)]  # type: ignore[union-attr]


def test_authentication_rejects_plan_symlink_before_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    cohort_root = data_root / "runs" / "cohort-auth"
    cohort_root.mkdir(parents=True)
    outside = tmp_path / "outside-plan.json"
    outside.write_text("{}")
    (cohort_root / "plan.json").symlink_to(outside)
    (cohort_root / "briefing.json").write_text("{}\n")
    monkeypatch.setattr(
        auth,
        "load_plan",
        lambda _path: pytest.fail("unsafe plan reached the loader"),
    )

    with pytest.raises(RuntimeAuthenticationError) as caught:
        auth.authenticate_plan_bundle(data_root, "cohort-auth")
    assert caught.value.code == "PLAN_PATH_UNSAFE"


def test_authentication_stops_on_profile_or_core_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, plan, state = _install_valid_bundle(tmp_path, monkeypatch)
    state["profile_sha"] = "0" * 64
    with pytest.raises(RuntimeAuthenticationError) as caught:
        auth.authenticate_plan_bundle(data_root, plan.cohort_id)
    assert caught.value.code == "SAFETY_PROFILE_DRIFT"
    assert state["source_checks"] == 0

    state["profile_sha"] = plan.safety_profile_sha256
    state["core_sha"] = "0" * 64
    with pytest.raises(RuntimeAuthenticationError) as caught:
        auth.authenticate_plan_bundle(data_root, plan.cohort_id)
    assert caught.value.code == "CORE_LOCK_DRIFT"
    assert state["source_checks"] == 0


def test_authentication_rejects_briefing_swapped_after_plan_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, plan, state = _install_valid_bundle(tmp_path, monkeypatch)
    monkeypatch.setattr(auth, "load_briefing", lambda _path: b'{"swapped":true}\n')

    with pytest.raises(RuntimeAuthenticationError) as caught:
        auth.authenticate_plan_bundle(data_root, plan.cohort_id)

    assert caught.value.code == "BRIEFING_DRIFT"
    assert state["source_checks"] == 0

def test_authentication_rejects_world_and_artifact_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, plan, state = _install_valid_bundle(tmp_path, monkeypatch)
    state["registration"] = SimpleNamespace(
        name=plan.world_id,
        repo=str(tmp_path / "world.git"),
        world_ref="refs/pmw/a-different-world",
    )
    with pytest.raises(RuntimeAuthenticationError) as caught:
        auth.authenticate_plan_bundle(data_root, plan.cohort_id)
    assert caught.value.code == "WORLD_REGISTRATION_MISMATCH"

    state["registration"] = SimpleNamespace(
        name=plan.world_id,
        repo=str(tmp_path / "world.git"),
        world_ref=plan.world_ref,
    )
    state["missing"] = (ARTIFACT_REF,)
    with pytest.raises(RuntimeAuthenticationError) as caught:
        auth.authenticate_plan_bundle(data_root, plan.cohort_id)
    assert caught.value.code == "ARTIFACT_CLOSURE_INVALID"


def test_authentication_rebuilds_the_exact_frozen_briefing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, plan, state = _install_valid_bundle(tmp_path, monkeypatch)
    state["rebuilt_briefing"] = b'{"schema":"WRONG_GENERATOR_OUTPUT"}\n'

    with pytest.raises(RuntimeAuthenticationError) as caught:
        auth.authenticate_plan_bundle(data_root, plan.cohort_id)

    assert caught.value.code == "BRIEFING_RECONSTRUCTION_MISMATCH"


def test_loaded_pmw_source_rejects_drift_in_importable_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "pmw-checkout"
    package = checkout / "pmw_r2"
    package.mkdir(parents=True)
    (checkout / ".git").mkdir()
    origin = package / "__init__.py"
    origin.write_text("")
    commit = "1" * 40
    lock = SimpleNamespace(
        source=lambda _name: SimpleNamespace(commit=commit),
    )
    monkeypatch.setattr(
        auth.importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin=str(origin)),
    )
    results = iter(
        (
            SimpleNamespace(returncode=0, stdout=(commit + "\n").encode()),
            SimpleNamespace(returncode=1, stdout=b""),
            SimpleNamespace(returncode=0, stdout=b""),
        )
    )
    monkeypatch.setattr(auth.subprocess, "run", lambda *_args, **_kwargs: next(results))

    with pytest.raises(RuntimeAuthenticationError) as caught:
        auth._validate_loaded_pmw_source(lock)  # type: ignore[arg-type]
    assert caught.value.code == "PMW_CORE_IDENTITY_MISMATCH"


def test_loaded_pmw_source_ignores_unrelated_checkout_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "pmw-checkout"
    package = checkout / "src" / "pmw_r2"
    package.mkdir(parents=True)
    (checkout / ".git").mkdir()
    origin = package / "__init__.py"
    origin.write_text("")
    commit = "2" * 40
    lock = SimpleNamespace(
        source=lambda _name: SimpleNamespace(commit=commit),
    )
    monkeypatch.setattr(
        auth.importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin=str(origin)),
    )
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> object:
        commands.append(command)
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout=(commit + "\n").encode())
        # A dirty README is intentionally not queried.  Both package-scoped
        # checks report that src/pmw_r2 is still identical to HEAD.
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(auth.subprocess, "run", run)

    auth._validate_loaded_pmw_source(lock)  # type: ignore[arg-type]

    assert len(commands) == 3
    assert commands[1][-1] == "src/pmw_r2"
    assert commands[2][-1] == "src/pmw_r2"
    assert all("status" not in command for command in commands)
