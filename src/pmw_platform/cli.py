"""Owner-facing command line for worlds and cohort plans."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sys
from typing import Awaitable, Sequence, TypeVar

from .artifacts import ArtifactStore, ArtifactStoreError
from .config import (
    ConfigError,
    WorldRegistration,
    WorldRegistry,
    default_data_root,
)
from .runtime.auth import RuntimeAuthenticationError, authenticate_plan_bundle
from .runtime.command import CommandBackendError, load_command_backend
from .runtime.orchestrator import (
    RuntimeLimits,
    RuntimeOrchestrationError,
    run_prepared_cohort,
)
from .runtime.publish import PmwContributionPublisher
from .runtime.safety import SafetyProfileError, load_named_profile
from .runtime.store import RuntimeStore, RuntimeStoreError
from .sessions import CohortPlan, PlanStoreError, plan_sha256, save_plan
from .source_lock import SourceLockError, load_core_lock
from .world import (
    ResearchWorld,
    ResearchWorldError,
    build_legacy_frontier_view,
    build_mathematical_situation,
    load_writer_authority,
)


class CommandLineError(ValueError):
    """A concise argument error rendered through the common JSON boundary."""


_T = TypeVar("_T")


async def _with_latched_sigint(operation: Awaitable[_T]) -> _T:
    """Turn every Ctrl-C during settlement into the same cancellation request.

    ``asyncio.run`` otherwise escalates a second SIGINT to a synchronous
    ``KeyboardInterrupt`` which can tear down shielded process cleanup.  The
    runtime already handles repeated task cancellation, so this owner-facing
    wrapper keeps SIGINT cooperative until a durable settlement exists.
    """

    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("runtime task is unavailable")
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    installed = False

    def request_stop(_signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(task.cancel)

    try:
        if previous is not signal.SIG_IGN:
            try:
                signal.signal(signal.SIGINT, request_stop)
                installed = True
            except ValueError:
                # A library caller may invoke the CLI from a non-main thread;
                # asyncio's normal cancellation semantics remain available.
                pass
        return await operation
    finally:
        if installed:
            signal.signal(signal.SIGINT, previous)


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


def _positive_number(value: str) -> float:
    try:
        selected = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not 0 < selected < float("inf"):
        raise argparse.ArgumentTypeError("must be positive and finite")
    return selected


_COHORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _emit(value: object, *, stream: object | None = None) -> None:
    selected_stream = sys.stdout if stream is None else stream
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ),
        file=selected_stream,
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


def _runtime_data_root(args: argparse.Namespace) -> Path:
    selected = default_data_root() if args.data_root is None else args.data_root
    expanded = selected.expanduser()
    if not expanded.is_absolute():
        expanded = Path(os.path.abspath(expanded))
    # Preserve an explicitly supplied symlink for the authentication/store
    # boundary to reject.  Resolving it here would silently erase the spelling
    # those strict readers are designed to validate.
    return expanded


def _runtime_cohort_id(value: object) -> str:
    if type(value) is not str or _COHORT_ID.fullmatch(value) is None:
        raise CommandLineError("cohort must be a canonical cohort ID")
    return value


def _session_start(args: argparse.Namespace) -> int:
    """The sole explicit path from a saved plan to external backend work."""

    cohort_id = _runtime_cohort_id(args.cohort)
    prepared = authenticate_plan_bundle(_runtime_data_root(args), cohort_id)
    if args.backend == "command":
        backend = load_command_backend(args.backend_config)
    elif args.backend == "pi":
        # Lazy import keeps read-only/status operations independent of a Pi
        # installation and cannot accidentally start a provider request.
        from .runtime.pi import load_pi_backend

        backend = load_pi_backend(args.backend_config)
    else:  # argparse choices make this unreachable.
        raise CommandLineError("unsupported runtime backend")

    publisher = None
    if args.writer_authority is not None:
        publisher = PmwContributionPublisher.create(
            prepared,
            load_writer_authority(args.writer_authority),
        )
    limits = RuntimeLimits(
        startup_seconds=args.startup_seconds,
        session_wall_seconds=(
            None if args.no_wall_limit else args.wall_seconds
        ),
        stop_grace_seconds=args.stop_grace_seconds,
    )
    try:
        result = asyncio.run(
            _with_latched_sigint(
                run_prepared_cohort(
                    prepared,
                    backend,
                    limits=limits,
                    publisher=publisher,
                )
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        status = RuntimeStore(prepared.cohort_root).read_status()
        _emit(
            {
                "schema": "PMW_RUNTIME_INTERRUPTED_1",
                "cohort_id": prepared.plan.cohort_id,
                "status": status,
            },
            stream=sys.stderr,
        )
        return 130
    _emit({
        "schema": "PMW_RUNTIME_START_RESULT_1",
        "cohort_id": prepared.plan.cohort_id,
        "launch_sha256": result.launch_sha256,
        "settlement_sha256": result.settlement_sha256,
        "outcome": result.outcome,
        "counts": result.settlement["counts"],
        "runtime_root": str(prepared.cohort_root / "runtime"),
    })
    return 0 if result.outcome == "SUCCEEDED" else 1


def _session_status(args: argparse.Namespace) -> None:
    cohort_id = _runtime_cohort_id(args.cohort)
    cohort_root = _runtime_data_root(args) / "runs" / cohort_id
    status = RuntimeStore(cohort_root).read_status()
    _emit({
        "schema": "PMW_RUNTIME_STATUS_1",
        "cohort_id": cohort_id,
        "runtime": status,
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

    session_start = session_commands.add_parser(
        "start",
        help="explicitly run one authenticated cohort through a selected backend",
    )
    session_start.add_argument("--cohort", required=True)
    session_start.add_argument(
        "--backend", choices=("command", "pi"), required=True
    )
    session_start.add_argument("--backend-config", type=Path, required=True)
    session_start.add_argument("--writer-authority", type=Path)
    session_start.add_argument(
        "--startup-seconds", type=_positive_number, default=60.0
    )
    wall = session_start.add_mutually_exclusive_group()
    wall.add_argument(
        "--wall-seconds", type=_positive_number, default=86_400.0
    )
    wall.add_argument(
        "--no-wall-limit",
        action="store_true",
        help="do not impose a host session wall limit",
    )
    session_start.add_argument(
        "--stop-grace-seconds", type=_positive_number, default=10.0
    )
    session_start.set_defaults(handler=_session_start)

    session_status = session_commands.add_parser(
        "status", help="read one launch without starting or resuming it"
    )
    session_status.add_argument("--cohort", required=True)
    session_status.set_defaults(handler=_session_status)

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
        result = args.handler(args)
    except (
        ArtifactStoreError,
        CommandLineError,
        ConfigError,
        PlanStoreError,
        ResearchWorldError,
        RuntimeAuthenticationError,
        RuntimeOrchestrationError,
        RuntimeStoreError,
        CommandBackendError,
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
    return result if type(result) is int else 0
