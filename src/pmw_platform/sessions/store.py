"""Owner-side persistence for immutable cohort plans."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile

from .model import CohortPlan


class PlanStoreError(ValueError):
    """A plan path is unsafe, already occupied, or malformed."""


MAXIMUM_PLAN_BYTES = 8 * 1024 * 1024
MAXIMUM_BRIEFING_BYTES = 16 * 1024 * 1024


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def canonical_plan_bytes(plan: CohortPlan) -> bytes:
    return plan.to_bytes()


def plan_sha256(plan: CohortPlan) -> str:
    return plan.sha256


def _validate_briefing_identity(plan: CohortPlan, raw: bytes) -> None:
    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise PlanStoreError(f"duplicate briefing key: {key}")
            result[key] = value
        return result

    def finite_float(value: str) -> float:
        selected = float(value)
        if not math.isfinite(selected):
            raise PlanStoreError("briefing contains a non-finite number")
        return selected

    def reject_constant(value: str) -> object:
        raise PlanStoreError(f"briefing contains a non-finite value: {value}")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_float=finite_float,
            parse_constant=reject_constant,
        )
    except PlanStoreError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise PlanStoreError("briefing JSON is invalid") from error
    if type(value) is not dict:
        raise PlanStoreError("briefing root must be an object")
    canonical = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if canonical != raw:
        raise PlanStoreError("briefing JSON is not canonical")
    expected = {
        "schema": "PMW_MATHEMATICAL_SITUATION_1",
        "world_id": plan.world_id,
        "world_ref": plan.world_ref,
        "snapshot_ref": plan.base_snapshot_ref,
    }
    if any(value.get(key) != selected for key, selected in expected.items()):
        raise PlanStoreError("briefing identity does not match cohort plan")


def save_plan(
    plan: CohortPlan,
    data_root: str | os.PathLike[str],
    *,
    briefing_bytes: bytes,
) -> Path:
    """Persist one plan and the exact briefing bound by its identity."""

    if not isinstance(plan, CohortPlan):
        raise PlanStoreError("plan must be a CohortPlan")
    if (
        type(briefing_bytes) is not bytes
        or not briefing_bytes
        or len(briefing_bytes) > MAXIMUM_BRIEFING_BYTES
    ):
        raise PlanStoreError("briefing size is invalid")
    import hashlib

    if hashlib.sha256(briefing_bytes).hexdigest() != plan.briefing_sha256:
        raise PlanStoreError("briefing digest does not match cohort plan")
    _validate_briefing_identity(plan, briefing_bytes)

    supplied_root = Path(data_root).expanduser()
    try:
        if supplied_root.is_symlink():
            raise PlanStoreError("data root must not be a symlink")
        supplied_root.mkdir(parents=True, exist_ok=True)
        root = supplied_root.resolve(strict=True)
        runs = root / "runs"
        if runs.is_symlink():
            raise PlanStoreError("runs directory must not be a symlink")
        runs.mkdir(exist_ok=True)
        if runs.resolve(strict=True) != runs or runs.parent != root:
            raise PlanStoreError("runs directory escaped the data root")
    except PlanStoreError:
        raise
    except OSError as error:
        raise PlanStoreError("cannot prepare cohort data root") from error
    cohort_root = runs / plan.cohort_id
    try:
        if cohort_root.exists() or cohort_root.is_symlink():
            raise PlanStoreError(f"cohort already exists: {plan.cohort_id}")
        staging_root = Path(
            tempfile.mkdtemp(prefix=f".{plan.cohort_id}.", dir=runs)
        )
        os.chmod(staging_root, 0o700)
        if staging_root.resolve(strict=True).parent != runs:
            raise PlanStoreError("cohort staging directory escaped runs")
    except PlanStoreError:
        raise
    except OSError as error:
        raise PlanStoreError("cannot create cohort staging directory") from error
    destination = staging_root / "plan.json"
    briefing_destination = staging_root / "briefing.json"
    temporary_paths: list[str] = []
    published = False
    try:
        for name, raw in (
            ("plan", canonical_plan_bytes(plan)),
            ("briefing", briefing_bytes),
        ):
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{name}.", suffix=".tmp", dir=staging_root
            )
            temporary_paths.append(temporary)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary_paths[0], destination)
        temporary_paths.pop(0)
        os.replace(temporary_paths[0], briefing_destination)
        temporary_paths.pop(0)
        _fsync_directory(staging_root)
        try:
            os.rename(staging_root, cohort_root)
        except OSError as error:
            if cohort_root.exists() or cohort_root.is_symlink():
                raise PlanStoreError(
                    f"cohort already exists: {plan.cohort_id}"
                ) from error
            raise
        published = True
        _fsync_directory(runs)
    except BaseException as error:
        for temporary in temporary_paths:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        if not published:
            for partial in (destination, briefing_destination):
                try:
                    partial.unlink()
                except FileNotFoundError:
                    pass
            try:
                staging_root.rmdir()
            except OSError:
                pass
        if isinstance(error, PlanStoreError):
            raise
        raise PlanStoreError("cannot persist cohort bundle") from error
    return cohort_root / "plan.json"


def load_briefing(plan_path: str | os.PathLike[str]) -> bytes:
    """Load the exact sibling briefing without following a symlink."""

    plan = Path(plan_path).expanduser()
    selected = plan.parent / "briefing.json"
    try:
        if selected.is_symlink() or not selected.is_file():
            raise PlanStoreError("cohort briefing is unavailable")
        size = selected.stat().st_size
        if not 1 <= size <= MAXIMUM_BRIEFING_BYTES:
            raise PlanStoreError("briefing size is invalid")
        return selected.read_bytes()
    except PlanStoreError:
        raise
    except OSError as error:
        raise PlanStoreError("cannot read cohort briefing") from error


def load_plan(path: str | os.PathLike[str]) -> CohortPlan:
    supplied = Path(path).expanduser()
    try:
        if supplied.is_symlink():
            raise PlanStoreError(f"plan must not be a symlink: {supplied}")
        selected = supplied.resolve(strict=True)
        if (
            not selected.is_file()
            or not 1 <= selected.stat().st_size <= MAXIMUM_PLAN_BYTES
        ):
            raise PlanStoreError(f"plan is not a bounded regular file: {selected}")
    except PlanStoreError:
        raise
    except OSError as error:
        raise PlanStoreError(f"cannot resolve plan: {supplied}") from error

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise PlanStoreError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise PlanStoreError(f"non-finite JSON value: {value}")

    def reject_float(_value: str) -> object:
        raise PlanStoreError("floating-point plan values are unsupported")

    try:
        raw = selected.read_bytes()
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_float=reject_float,
            parse_constant=reject_constant,
        )
    except PlanStoreError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise PlanStoreError(f"cannot read plan: {selected}") from error
    if not isinstance(value, dict):
        raise PlanStoreError("plan root must be an object")
    try:
        plan = CohortPlan.from_manifest(value)
    except ValueError as error:
        raise PlanStoreError(str(error)) from error
    if plan.to_bytes() != raw:
        raise PlanStoreError("plan JSON is not canonical")
    briefing = load_briefing(selected)
    import hashlib

    if hashlib.sha256(briefing).hexdigest() != plan.briefing_sha256:
        raise PlanStoreError("briefing digest does not match cohort plan")
    _validate_briefing_identity(plan, briefing)
    return plan
