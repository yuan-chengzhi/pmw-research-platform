"""Owner-facing command line for worlds and cohort plans."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys
from typing import Sequence

from .artifacts import ArtifactStore, ArtifactStoreError
from .config import ConfigError, WorldRegistration, WorldRegistry
from .runtime.safety import SafetyProfileError, load_named_profile
from .sessions import CohortPlan, PlanStoreError, plan_sha256, save_plan
from .source_lock import SourceLockError, load_core_lock
from .world import (
    ResearchWorld,
    ResearchWorldError,
    build_legacy_frontier_view,
    build_mathematical_situation,
)


class CommandLineError(ValueError):
    """A concise argument error rendered through the common JSON boundary."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CommandLineError(message)


def _bounded_integer(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            selected = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("must be an integer") from error
        if not minimum <= selected <= maximum:
            raise argparse.ArgumentTypeError(
                f"must be between {minimum} and {maximum}"
            )
        return selected

    return parse


def _emit(value: object, *, stream: object = sys.stdout) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ),
        file=stream,
    )


def _registry(args: argparse.Namespace) -> WorldRegistry:
    return WorldRegistry(args.data_root)


def _open_registered(
    args: argparse.Namespace,
) -> tuple[WorldRegistration, ResearchWorld]:
    registration = _registry(args).get(args.name)
    world = ResearchWorld.open(
        registration.repo,
        world_id=registration.name,
        world_ref=registration.world_ref,
        required_snapshot_ref=registration.seed_snapshot_ref,
    )
    return registration, world


def _world_list(args: argparse.Namespace) -> None:
    _emit({
        "schema": "PMW_RESEARCH_WORLD_LIST_1",
        "worlds": [
            {
                "name": row.name,
                "repo": row.repo,
                "world_ref": row.world_ref,
                "seed_snapshot_ref": row.seed_snapshot_ref,
                "registered_at": row.registered_at,
            }
            for row in _registry(args).list()
        ],
    })


def _world_add(args: argparse.Namespace) -> None:
    world = ResearchWorld.open(
        args.repo,
        world_id=args.name,
        world_ref=args.world_ref,
        required_snapshot_ref=args.snapshot,
    )
    head = world.head()
    records = world.records(head)
    legacy = build_legacy_frontier_view(world, snapshot_ref=head)
    registration = WorldRegistration.create(
        name=args.name,
        repo=args.repo,
        world_ref=args.world_ref,
        seed_snapshot_ref=args.snapshot,
    )
    _registry(args).add(registration, replace=args.replace)
    _emit({
        "schema": "PMW_RESEARCH_WORLD_ADDED_1",
        "name": registration.name,
        "repo": registration.repo,
        "world_ref": registration.world_ref,
        "seed_snapshot_ref": registration.seed_snapshot_ref,
        "head_snapshot_ref": head,
        "validated": True,
        "record_count": len(records),
        "problem_count": len(legacy.problems),
    })


def _world_status(args: argparse.Namespace) -> None:
    registration, world = _open_registered(args)
    head = world.head()
    legacy = build_legacy_frontier_view(world, snapshot_ref=head)
    _emit({
        "schema": "PMW_RESEARCH_WORLD_STATUS_1",
        "name": registration.name,
        "repo": registration.repo,
        "world_ref": registration.world_ref,
        "seed_snapshot_ref": registration.seed_snapshot_ref,
        "head_snapshot_ref": head,
        "validated": True,
        "record_count": sum(legacy.schema_counts.values()),
        "problem_count": len(legacy.problems),
        "legacy_schema_counts": legacy.schema_counts,
        "unscoped_record_count": len(legacy.unscoped_admission_refs),
    })


def _world_audit(args: argparse.Namespace) -> None:
    registration, world = _open_registered(args)
    audit = world.audit()
    _emit({
        "schema": "PMW_RESEARCH_WORLD_AUDIT_1",
        "name": registration.name,
        "world_ref": registration.world_ref,
        "seed_snapshot_ref": registration.seed_snapshot_ref,
        "head_snapshot_ref": audit.get("snapshot_ref"),
        "audit": audit,
    })


def _world_delta(args: argparse.Namespace) -> None:
    _registration, world = _open_registered(args)
    selected_snapshot = world.head() if args.snapshot is None else args.snapshot
    rows = world.delta(args.since, selected_snapshot)
    selected = rows[: args.limit]
    _emit({
        "schema": "PMW_RESEARCH_WORLD_DELTA_1",
        "since_snapshot_ref": args.since,
        "snapshot_ref": selected_snapshot,
        "total_records": len(rows),
        "returned_records": len(selected),
        "truncated": len(selected) != len(rows),
        "records": [
            {
                "admission_ref": row.admission_ref,
                "schema": row.schema,
                "parent_refs": list(row.parent_refs),
            }
            for row in selected
        ],
    })


def _world_get(args: argparse.Namespace) -> None:
    _registration, world = _open_registered(args)
    selected_snapshot = world.head() if args.snapshot is None else args.snapshot
    row = world.get(args.admission, selected_snapshot)
    _emit({
        "schema": "PMW_RESEARCH_WORLD_RECORD_1",
        "snapshot_ref": selected_snapshot,
        "record": row.to_value(),
    })


def _world_briefing(args: argparse.Namespace) -> None:
    registration, world = _open_registered(args)
    store = ArtifactStore(_registry(args).data_root)
    selected_snapshot = world.head() if args.snapshot is None else args.snapshot
    situation = build_mathematical_situation(
        world,
        world_id=registration.name,
        snapshot_ref=selected_snapshot,
        artifact_exists=store.exists,
    )
    _emit({
        "schema": "PMW_RESEARCH_BRIEFING_1",
        "briefing_sha256": situation.sha256,
        "briefing_bytes": len(situation.bytes),
        "briefing": situation.value,
    })


def _cohort_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"cohort-{timestamp}-{secrets.token_hex(4)}"


def _session_plan(args: argparse.Namespace) -> None:
    registration, world = _open_registered(args)
    profile = load_named_profile(args.profile)
    core_lock = load_core_lock()
    store = ArtifactStore(_registry(args).data_root)
    missing_artifacts: set[str] = set()

    def artifact_exists(reference: str) -> bool:
        available = store.exists(reference)
        if not available:
            missing_artifacts.add(reference)
        return available

    base_snapshot_ref = world.head()
    situation = build_mathematical_situation(
        world,
        world_id=registration.name,
        snapshot_ref=base_snapshot_ref,
        artifact_exists=artifact_exists,
    )
    if missing_artifacts:
        raise ArtifactStoreError(
            "WORLD_ARTIFACTS_MISSING",
            f"{len(missing_artifacts)} unresolved artifact refs",
        )
    plan = CohortPlan.generate(
        cohort_id=args.cohort_id or _cohort_id(),
        world_id=registration.name,
        world_ref=registration.world_ref,
        base_snapshot_ref=base_snapshot_ref,
        safety_profile=profile.name,
        safety_profile_sha256=profile.sha256,
        core_lock_sha256=core_lock.sha256,
        briefing_sha256=situation.sha256,
        count=args.count,
        concurrency=args.concurrency,
    )
    path = save_plan(
        plan,
        _registry(args).data_root,
        briefing_bytes=situation.bytes,
    )
    _emit({
        "schema": "PMW_RESEARCH_COHORT_PLANNED_1",
        "plan_path": str(path),
        "plan_sha256": plan_sha256(plan),
        "cohort": plan.to_manifest(),
        "model_calls": 0,
    })


def _artifact_import(args: argparse.Namespace) -> None:
    imported = ArtifactStore(_registry(args).data_root).import_legacy(
        args.source, source_label=args.label
    )
    _emit({
        "schema": "PMW_RESEARCH_ARTIFACT_IMPORT_RESULT_1",
        "source_label": imported.source_label,
        "object_count": imported.object_count,
        "object_bytes": imported.object_bytes,
        "receipt_count": imported.receipt_count,
        "receipt_bytes": imported.receipt_bytes,
        "manifest_sha256": imported.manifest_sha256,
        "manifest_path": str(imported.manifest_path),
    })


def _artifact_audit(args: argparse.Namespace) -> None:
    _registration, world = _open_registered(args)
    snapshot = world.head()
    references: set[str] = set()
    for row in world.records(snapshot):
        stack = [row.content]
        while stack:
            value = stack.pop()
            if type(value) is str and value.startswith("artifact/sha256/"):
                references.add(value)
            elif type(value) is list:
                stack.extend(value)
            elif type(value) is dict:
                stack.extend(value.values())
    missing = ArtifactStore(_registry(args).data_root).audit_refs(references)
    _emit({
        "schema": "PMW_RESEARCH_ARTIFACT_AUDIT_1",
        "world": args.name,
        "snapshot_ref": snapshot,
        "referenced_objects": len(references),
        "resolved_objects": len(references) - len(missing),
        "missing_refs": list(missing),
        "valid": not missing,
    })


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="pmw-research",
        description="Operate long-lived mathematical worlds and session cohorts.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        help="runtime data root (default: PMW_RESEARCH_DATA_ROOT or ~/Documents/pmw-research-data)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    world = commands.add_parser("world", help="register and inspect worlds")
    world_commands = world.add_subparsers(dest="world_command", required=True)

    world_list = world_commands.add_parser("list", help="list registered worlds")
    world_list.set_defaults(handler=_world_list)

    world_add = world_commands.add_parser("add", help="audit and register an existing bare PMW world")
    world_add.add_argument("name")
    world_add.add_argument("--repo", type=Path, required=True)
    world_add.add_argument("--world-ref", required=True)
    world_add.add_argument("--snapshot", required=True)
    world_add.add_argument("--replace", action="store_true")
    world_add.set_defaults(handler=_world_add)

    world_status = world_commands.add_parser("status", help="audit one registered world")
    world_status.add_argument("name")
    world_status.set_defaults(handler=_world_status)

    world_delta = world_commands.add_parser("delta", help="list admissions added after a snapshot")
    world_delta.add_argument("name")
    world_delta.add_argument("--since", required=True)
    world_delta.add_argument("--snapshot")
    world_delta.add_argument(
        "--limit", type=_bounded_integer(1, 1_000), default=64
    )
    world_delta.set_defaults(handler=_world_delta)

    world_audit = world_commands.add_parser(
        "audit", help="independently reconstruct and audit the full PMW history"
    )
    world_audit.add_argument("name")
    world_audit.set_defaults(handler=_world_audit)

    world_get = world_commands.add_parser(
        "get", help="retrieve one exact admission at a snapshot"
    )
    world_get.add_argument("name")
    world_get.add_argument("--admission", required=True)
    world_get.add_argument("--snapshot")
    world_get.set_defaults(handler=_world_get)

    world_briefing = world_commands.add_parser(
        "briefing", help="render the deterministic mathematical situation"
    )
    world_briefing.add_argument("name")
    world_briefing.add_argument("--snapshot")
    world_briefing.set_defaults(handler=_world_briefing)

    session = commands.add_parser("session", help="plan research sessions")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    session_plan = session_commands.add_parser(
        "plan", help="freeze explicit session IDs without calling a model"
    )
    session_plan.add_argument("--world", dest="name", required=True)
    session_plan.add_argument(
        "--count", type=_bounded_integer(1, 4_096), required=True
    )
    session_plan.add_argument(
        "--concurrency", type=_bounded_integer(1, 4_096), required=True
    )
    session_plan.add_argument("--profile", default="research-default")
    session_plan.add_argument("--cohort-id")
    session_plan.set_defaults(handler=_session_plan)

    artifact = commands.add_parser(
        "artifact", help="import and audit content-addressed artifacts"
    )
    artifact_commands = artifact.add_subparsers(
        dest="artifact_command", required=True
    )
    artifact_import = artifact_commands.add_parser(
        "import", help="copy one validated historical artifact store"
    )
    artifact_import.add_argument("--source", type=Path, required=True)
    artifact_import.add_argument("--label", required=True)
    artifact_import.set_defaults(handler=_artifact_import)
    artifact_audit = artifact_commands.add_parser(
        "audit", help="verify every artifact ref reachable from a world snapshot"
    )
    artifact_audit.add_argument("--world", dest="name", required=True)
    artifact_audit.set_defaults(handler=_artifact_audit)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        args.handler(args)
    except (
        ArtifactStoreError,
        CommandLineError,
        ConfigError,
        PlanStoreError,
        ResearchWorldError,
        SafetyProfileError,
        SourceLockError,
        ValueError,
    ) as error:
        _emit(
            {
                "schema": "PMW_RESEARCH_COMMAND_ERROR_1",
                "error_type": type(error).__name__,
                "message": str(error),
            },
            stream=sys.stderr,
        )
        return 2
    return 0
