"""Authenticate a saved cohort bundle before any backend can start."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

from ..artifacts import ArtifactStore, ArtifactStoreError
from ..config import ConfigError, WorldRegistration, WorldRegistry
from ..sessions import CohortPlan, PlanStoreError, load_briefing, load_plan
from ..source_lock import CoreLock, SourceLockError, load_core_lock
from ..source_materializer import SourceMaterializer, SourceMaterializerError
from ..world import (
    ResearchWorld,
    ResearchWorldError,
    activate_pmw_core,
    build_mathematical_situation,
)
from .safety import SafetyProfile, SafetyProfileError, load_named_profile


class RuntimeAuthenticationError(ValueError):
    """The launch inputs do not match one complete saved authority bundle."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


@dataclass(frozen=True, slots=True)
class PreparedCohort:
    """Fully authenticated, read-only launch input for a runtime cohort."""

    data_root: Path
    cohort_root: Path
    plan_path: Path
    briefing_path: Path
    briefing_bytes: bytes
    plan: CohortPlan
    profile: SafetyProfile
    core_lock: CoreLock
    registration: WorldRegistration
    world: ResearchWorld
    artifact_store: ArtifactStore

    @property
    def plan_sha256(self) -> str:
        return self.plan.sha256


def _canonical_data_root(value: str | os.PathLike[str]) -> Path:
    supplied = Path(value).expanduser()
    try:
        if supplied.is_symlink():
            raise RuntimeAuthenticationError("DATA_ROOT_UNSAFE", "symlink")
        selected = supplied.resolve(strict=True)
    except RuntimeAuthenticationError:
        raise
    except OSError as error:
        raise RuntimeAuthenticationError("DATA_ROOT_UNAVAILABLE") from error
    if not selected.is_dir():
        raise RuntimeAuthenticationError("DATA_ROOT_UNSAFE", "not a directory")
    return selected


def _artifact_references(briefing: bytes) -> tuple[str, ...]:
    try:
        value = json.loads(briefing.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise RuntimeAuthenticationError("BRIEFING_INVALID") from error
    references: set[str] = set()
    stack = [value]
    while stack:
        selected = stack.pop()
        if type(selected) is str:
            if selected.startswith("artifact/sha256/"):
                references.add(selected)
        elif type(selected) is list:
            stack.extend(selected)
        elif type(selected) is dict:
            stack.extend(selected.values())
    return tuple(sorted(references))


def _activate_locked_pmw_source(data_root: Path, core_lock: CoreLock) -> None:
    """Audit and load PMW from the managed core-lock source materialization."""

    try:
        materialized = SourceMaterializer(
            data_root, core_lock=core_lock
        ).audit("persistent-mathematical-worlds")
        activate_pmw_core(
            materialized.tree_path,
            tree_sha256=materialized.tree_sha256,
        )
    except SourceMaterializerError as error:
        raise RuntimeAuthenticationError(
            "PMW_CORE_IDENTITY_UNPROVEN", error.code
        ) from error
    except ResearchWorldError as error:
        raise RuntimeAuthenticationError(error.code, error.detail) from error


def authenticate_plan_bundle(
    data_root: str | os.PathLike[str],
    cohort_id: str,
    *,
    profiles_dir: str | os.PathLike[str] | None = None,
    core_lock_path: str | os.PathLike[str] | None = None,
) -> PreparedCohort:
    """Resolve one canonical ``runs/<cohort>/plan.json`` and all authorities.

    There is intentionally no ``SessionSpec`` argument.  Every runnable
    session is derived from the authenticated plan returned here.
    """

    root = _canonical_data_root(data_root)
    if type(cohort_id) is not str or not cohort_id or "/" in cohort_id:
        raise RuntimeAuthenticationError("COHORT_ID_INVALID")
    cohort_root = root / "runs" / cohort_id
    expected_plan = cohort_root / "plan.json"
    try:
        if cohort_root.is_symlink() or expected_plan.is_symlink():
            raise RuntimeAuthenticationError("PLAN_PATH_UNSAFE", "symlink")
        resolved_cohort = cohort_root.resolve(strict=True)
        plan_path = expected_plan.resolve(strict=True)
    except RuntimeAuthenticationError:
        raise
    except OSError as error:
        raise RuntimeAuthenticationError("PLAN_BUNDLE_UNAVAILABLE") from error
    if (
        resolved_cohort != cohort_root
        or resolved_cohort.parent != root / "runs"
        or plan_path != resolved_cohort / "plan.json"
    ):
        raise RuntimeAuthenticationError("PLAN_PATH_UNSAFE", "escaped canonical path")

    try:
        plan = load_plan(plan_path)
        briefing = load_briefing(plan_path)
    except PlanStoreError as error:
        raise RuntimeAuthenticationError("PLAN_BUNDLE_INVALID", str(error)) from error
    if plan.cohort_id != cohort_id:
        raise RuntimeAuthenticationError("PLAN_COHORT_MISMATCH")
    if hashlib.sha256(plan.to_bytes()).hexdigest() != plan.sha256:
        raise AssertionError("plan digest property drift")
    # ``load_plan`` validates its own briefing read, but the runtime retains a
    # later no-follow read.  Bind those exact retained bytes again so a swap
    # between the two reads cannot cross into SessionRequest.
    if hashlib.sha256(briefing).hexdigest() != plan.briefing_sha256:
        raise RuntimeAuthenticationError("BRIEFING_DRIFT")

    briefing_path = resolved_cohort / "briefing.json"
    try:
        if briefing_path.is_symlink():
            raise RuntimeAuthenticationError("PLAN_PATH_UNSAFE", "briefing symlink")
        if briefing_path.resolve(strict=True) != briefing_path:
            raise RuntimeAuthenticationError("PLAN_PATH_UNSAFE", "briefing escaped")
    except RuntimeAuthenticationError:
        raise
    except OSError as error:
        raise RuntimeAuthenticationError("PLAN_BUNDLE_UNAVAILABLE") from error

    try:
        profile = load_named_profile(plan.safety_profile, profiles_dir=profiles_dir)
    except SafetyProfileError as error:
        raise RuntimeAuthenticationError(
            "SAFETY_PROFILE_INVALID", str(error)
        ) from error
    if profile.sha256 != plan.safety_profile_sha256:
        raise RuntimeAuthenticationError("SAFETY_PROFILE_DRIFT")
    try:
        core_lock = load_core_lock(core_lock_path)
    except SourceLockError as error:
        raise RuntimeAuthenticationError("CORE_LOCK_INVALID", str(error)) from error
    if core_lock.sha256 != plan.core_lock_sha256:
        raise RuntimeAuthenticationError("CORE_LOCK_DRIFT")
    _activate_locked_pmw_source(root, core_lock)

    try:
        registration = WorldRegistry(root).get(plan.world_id)
    except ConfigError as error:
        raise RuntimeAuthenticationError(
            "WORLD_REGISTRATION_INVALID", str(error)
        ) from error
    if (
        registration.name != plan.world_id
        or registration.world_ref != plan.world_ref
    ):
        raise RuntimeAuthenticationError("WORLD_REGISTRATION_MISMATCH")
    try:
        world = ResearchWorld.open(
            registration.repo,
            world_id=registration.name,
            world_ref=registration.world_ref,
            required_snapshot_ref=plan.base_snapshot_ref,
        )
        head = world.head()
        world.delta(plan.base_snapshot_ref, head)
    except ResearchWorldError as error:
        raise RuntimeAuthenticationError("WORLD_SNAPSHOT_INVALID", str(error)) from error

    store = ArtifactStore(root)
    try:
        rebuilt = build_mathematical_situation(
            world,
            world_id=registration.name,
            snapshot_ref=plan.base_snapshot_ref,
            artifact_exists=store.exists,
        )
    except (ResearchWorldError, ArtifactStoreError) as error:
        raise RuntimeAuthenticationError(
            "BRIEFING_RECONSTRUCTION_FAILED", str(error)
        ) from error
    if rebuilt.sha256 != plan.briefing_sha256 or rebuilt.bytes != briefing:
        raise RuntimeAuthenticationError("BRIEFING_RECONSTRUCTION_MISMATCH")
    references = _artifact_references(briefing)
    try:
        missing = store.audit_refs(references)
    except ArtifactStoreError as error:
        raise RuntimeAuthenticationError("ARTIFACT_CLOSURE_INVALID", str(error)) from error
    if missing:
        raise RuntimeAuthenticationError(
            "ARTIFACT_CLOSURE_INVALID", f"{len(missing)} unresolved refs"
        )
    return PreparedCohort(
        data_root=root,
        cohort_root=resolved_cohort,
        plan_path=plan_path,
        briefing_path=briefing_path,
        briefing_bytes=briefing,
        plan=plan,
        profile=profile,
        core_lock=core_lock,
        registration=registration,
        world=world,
        artifact_store=store,
    )
