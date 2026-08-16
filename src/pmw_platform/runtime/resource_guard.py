"""Low-frequency, profile-bound resource accounting for runtime sessions.

The guard intentionally enforces only three aggregate tree limits: logical
bytes, directory entries, and depth.  It does not impose a per-file limit,
reject hard links, follow symbolic links, or continuously rescan a research
workspace unless the authenticated profile explicitly selects
``LIVE_LATCHED``.

All filesystem work stays in the cancellable asyncio task.  Scans yield after
a bounded number of entries and never leave an unjoinable worker thread.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Mapping

from .safety import Disposition, DiskGuard, SafetyProfile, TreeLimits
from .store import RuntimeStore


RESOURCE_EVIDENCE_SCHEMA = "PMW_RUNTIME_RESOURCE_EVIDENCE_1"
MAXIMUM_RESOURCE_WARNINGS = 4
SCAN_YIELD_ENTRIES = 256


class ResourceAccountingError(RuntimeError):
    """A resource measurement could not be completed reliably."""


@dataclass(frozen=True, slots=True)
class DiskSnapshot:
    """One bounded view of the filesystem containing the runtime."""

    total_bytes: int
    available_bytes: int
    required_free_bytes: int

    def to_value(self) -> dict[str, int]:
        return {
            "total_bytes": self.total_bytes,
            "available_bytes": self.available_bytes,
            "required_free_bytes": self.required_free_bytes,
        }


@dataclass(frozen=True, slots=True)
class TreeSnapshot:
    """Aggregate logical accounting for one workspace or cache tree."""

    total_bytes: int
    entries: int
    maximum_depth: int

    def to_value(self) -> dict[str, int]:
        return {
            "total_bytes": self.total_bytes,
            "entries": self.entries,
            "maximum_depth": self.maximum_depth,
        }


@dataclass(frozen=True, slots=True)
class ResourceEvent:
    """A bounded warning or terminal resource observation."""

    code: str
    scope: str
    target: str
    phase: str
    disposition: str | None
    session_id: str | None
    uncertain: bool
    observed: Mapping[str, int]
    limits: Mapping[str, int]
    detail: str = ""

    def to_value(self) -> dict[str, object]:
        return {
            "code": self.code,
            "scope": self.scope,
            "target": self.target,
            "phase": self.phase,
            "disposition": self.disposition,
            "session_id": self.session_id,
            "uncertain": self.uncertain,
            "observed": dict(self.observed),
            "limits": dict(self.limits),
            "detail": self.detail[:512],
        }


def _resource_error_detail(error: BaseException) -> str:
    return f"{type(error).__module__}.{type(error).__qualname__}"[:512]


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise ResourceAccountingError("tree root cannot be pinned") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ResourceAccountingError("tree root cannot be pinned")
    return metadata.st_dev, metadata.st_ino


def _directory_open_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", None)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if directory is None or no_follow is None:
        raise ResourceAccountingError("no-follow directory opens are unavailable")
    return os.O_RDONLY | directory | no_follow | getattr(os, "O_CLOEXEC", 0)


def _opened_directory_identity(descriptor: int) -> tuple[int, int]:
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise ResourceAccountingError("opened directory cannot be identified") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ResourceAccountingError("opened tree component is not a directory")
    return metadata.st_dev, metadata.st_ino


def _open_directory_chain(
    root_descriptor: int,
    chain: tuple[tuple[str, tuple[int, int]], ...],
    root_identity: tuple[int, int],
) -> int:
    """Open every relative component without following a pathname link."""

    try:
        current = os.dup(root_descriptor)
    except OSError as error:
        raise ResourceAccountingError("tree root descriptor cannot be duplicated") from error
    try:
        if _opened_directory_identity(current) != root_identity:
            raise ResourceAccountingError("tree root descriptor drifted")
        for name, expected_identity in chain:
            try:
                child = os.open(
                    name,
                    _directory_open_flags(),
                    dir_fd=current,
                )
            except OSError as error:
                raise ResourceAccountingError(
                    "tree component cannot be opened without following links"
                ) from error
            try:
                if _opened_directory_identity(child) != expected_identity:
                    raise ResourceAccountingError(
                        "tree component changed between discovery and traversal"
                    )
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


async def read_disk_snapshot(path: Path, guard: DiskGuard) -> DiskSnapshot:
    """Read available host bytes using the caller-visible filesystem quota."""

    try:
        value = os.statvfs(path)
        fragment = value.f_frsize or value.f_bsize
        total = value.f_blocks * fragment
        available = value.f_bavail * fragment
        required = guard.required_free_bytes(total)
    except (OSError, OverflowError, ValueError) as error:
        raise ResourceAccountingError("disk accounting failed") from error
    if total <= 0 or available < 0 or available > total:
        raise ResourceAccountingError("disk accounting returned invalid values")
    return DiskSnapshot(
        total_bytes=total,
        available_bytes=available,
        required_free_bytes=required,
    )


async def scan_tree(
    root: Path,
    limits: TreeLimits,
    *,
    expected_root_identity: tuple[int, int] | None = None,
    yield_every_entries: int = SCAN_YIELD_ENTRIES,
) -> TreeSnapshot:
    """Account one tree without following links or penalizing hard links.

    Regular-file logical bytes are counted once per ``(device, inode)``.
    Directory entries, including additional hard-link names and symlinks, are
    counted individually.  Traversal is rooted in a no-follow directory FD;
    every nested component is reopened relative to that FD and its discovered
    inode is rechecked before enumeration.
    """

    if type(yield_every_entries) is not int or yield_every_entries <= 0:
        raise ValueError("yield_every_entries must be a positive integer")
    try:
        root_descriptor = os.open(root, _directory_open_flags())
    except OSError as error:
        raise ResourceAccountingError(
            "tree root cannot be opened without following links"
        ) from error
    try:
        root_identity = _opened_directory_identity(root_descriptor)
        if (
            expected_root_identity is not None
            and root_identity != expected_root_identity
        ):
            raise ResourceAccountingError("tree root changed between scans")

        total_bytes = 0
        entries = 0
        maximum_depth = 0
        seen_files: set[tuple[int, int]] = set()
        seen_directories = {root_identity}
        pending: list[
            tuple[tuple[tuple[str, tuple[int, int]], ...], int]
        ] = [((), 0)]

        while pending:
            chain, parent_depth = pending.pop()
            directory_descriptor = _open_directory_chain(
                root_descriptor,
                chain,
                root_identity,
            )
            try:
                try:
                    iterator = os.scandir(directory_descriptor)
                except OSError as error:
                    raise ResourceAccountingError(
                        "directory descriptor cannot be scanned"
                    ) from error
                with iterator:
                    for entry in iterator:
                        if type(entry.name) is not str:
                            raise ResourceAccountingError(
                                "directory entry name is not text"
                            )
                        try:
                            metadata = os.stat(
                                entry.name,
                                dir_fd=directory_descriptor,
                                follow_symlinks=False,
                            )
                        except OSError as error:
                            raise ResourceAccountingError(
                                "entry cannot be accounted from its directory FD"
                            ) from error

                        entries += 1
                        depth = parent_depth + 1
                        maximum_depth = max(maximum_depth, depth)
                        mode = metadata.st_mode
                        identity = (metadata.st_dev, metadata.st_ino)
                        if stat.S_ISREG(mode) and identity not in seen_files:
                            seen_files.add(identity)
                            total_bytes += metadata.st_size
                        elif stat.S_ISDIR(mode) and identity not in seen_directories:
                            seen_directories.add(identity)
                            pending.append(
                                (chain + ((entry.name, identity),), depth)
                            )

                        # Once a limit is proven crossed, a complete expensive
                        # traversal is unnecessary.  Re-check the named root
                        # before returning the crossing observation.
                        if (
                            total_bytes > limits.maximum_total_bytes
                            or entries > limits.maximum_entries
                            or maximum_depth > limits.maximum_depth
                        ):
                            if _directory_identity(root) != root_identity:
                                raise ResourceAccountingError(
                                    "tree root changed during scan"
                                )
                            return TreeSnapshot(
                                total_bytes=total_bytes,
                                entries=entries,
                                maximum_depth=maximum_depth,
                            )
                        if entries % yield_every_entries == 0:
                            await asyncio.sleep(0)
            except ResourceAccountingError:
                raise
            except OSError as error:
                raise ResourceAccountingError("tree iteration failed") from error
            finally:
                os.close(directory_descriptor)

        if _directory_identity(root) != root_identity:
            raise ResourceAccountingError("tree root changed during scan")
        return TreeSnapshot(
            total_bytes=total_bytes,
            entries=entries,
            maximum_depth=maximum_depth,
        )
    finally:
        os.close(root_descriptor)


class ResourceGuard:
    """Own all low-frequency monitors for one runtime cohort."""

    def __init__(
        self,
        profile: SafetyProfile,
        store: RuntimeStore,
        session_ids: tuple[str, ...],
    ) -> None:
        if not isinstance(profile, SafetyProfile):
            raise TypeError("profile must be SafetyProfile")
        if not isinstance(store, RuntimeStore):
            raise TypeError("store must be RuntimeStore")
        if not session_ids or len(set(session_ids)) != len(session_ids):
            raise ValueError("session_ids must be nonempty and unique")
        self.profile = profile
        self.store = store
        self.session_ids = session_ids
        self._paths = {
            session_id: store.session_paths(session_id)
            for session_id in session_ids
        }
        self._root_identity = {
            session_id: {
                target: _directory_identity(getattr(paths, target))
                for target in ("workspace", "cache")
            }
            for session_id, paths in self._paths.items()
        }
        self._events = {
            session_id: asyncio.Event() for session_id in session_ids
        }
        self._session_terminal: dict[str, ResourceEvent] = {}
        self._global_terminal: ResourceEvent | None = None
        self._warnings: dict[str, list[ResourceEvent]] = {
            session_id: [] for session_id in session_ids
        }
        self._latest_disk: DiskSnapshot | None = None
        self._latest_tree: dict[str, dict[str, TreeSnapshot | None]] = {
            session_id: {"workspace": None, "cache": None}
            for session_id in session_ids
        }
        self._tree_checks: dict[str, dict[str, int]] = {
            session_id: {"workspace": 0, "cache": 0}
            for session_id in session_ids
        }
        self._disk_checks = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._session_tasks: dict[str, set[asyncio.Task[None]]] = {
            session_id: set() for session_id in session_ids
        }
        self._activated: set[str] = set()
        self._finished: set[str] = set()
        self._started = False
        self._closed = False

    @property
    def active_task_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    @property
    def global_event(self) -> ResourceEvent | None:
        return self._global_terminal

    def event_for(self, session_id: str) -> ResourceEvent | None:
        self._require_session(session_id)
        return self._global_terminal or self._session_terminal.get(session_id)

    def _require_session(self, session_id: str) -> None:
        if session_id not in self._events:
            raise KeyError(session_id)

    def _latch(self, event: ResourceEvent) -> None:
        if event.disposition == Disposition.WARN.value and not event.uncertain:
            targets = (
                self.session_ids if event.session_id is None else (event.session_id,)
            )
            for session_id in targets:
                warnings = self._warnings[session_id]
                if len(warnings) < MAXIMUM_RESOURCE_WARNINGS:
                    warnings.append(event)
            return

        if event.scope == "COHORT":
            if self._global_terminal is None:
                self._global_terminal = event
                for signal in self._events.values():
                    signal.set()
            return
        if event.session_id is None:
            raise AssertionError("session-scoped resource event lacks a session")
        if event.session_id not in self._session_terminal:
            self._session_terminal[event.session_id] = event
            self._events[event.session_id].set()

    def _uncertain(
        self,
        *,
        target: str,
        phase: str,
        session_id: str | None,
        error: BaseException,
    ) -> ResourceEvent:
        event = ResourceEvent(
            code="RESOURCE_ACCOUNTING_UNCERTAIN",
            scope="COHORT",
            target=target,
            phase=phase,
            disposition=None,
            session_id=session_id,
            uncertain=True,
            observed={},
            limits={},
            detail=_resource_error_detail(error),
        )
        self._latch(event)
        return event

    async def _check_disk(self, phase: str) -> ResourceEvent | None:
        self._disk_checks += 1
        try:
            snapshot = await read_disk_snapshot(
                self.store.runtime_root, self.profile.disk_guard
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            return self._uncertain(
                target="host_filesystem",
                phase=phase,
                session_id=None,
                error=error,
            )
        self._latest_disk = snapshot
        if snapshot.available_bytes >= snapshot.required_free_bytes:
            return None
        event = ResourceEvent(
            code="DISK_RESERVE_BREACHED",
            scope="COHORT",
            target="host_filesystem",
            phase=phase,
            disposition=self.profile.disposition(
                "DISK_RESERVE_BREACHED"
            ).value,
            session_id=None,
            uncertain=False,
            observed={"available_bytes": snapshot.available_bytes},
            limits={"required_free_bytes": snapshot.required_free_bytes},
        )
        self._latch(event)
        return event

    @staticmethod
    def _tree_limit_event(
        profile: SafetyProfile,
        session_id: str,
        target: str,
        phase: str,
        snapshot: TreeSnapshot,
        limits: TreeLimits,
    ) -> ResourceEvent | None:
        prefix = "WORKSPACE" if target == "workspace" else "RUNTIME_CACHE"
        rows = (
            (
                snapshot.total_bytes,
                limits.maximum_total_bytes,
                f"{prefix}_TOTAL_BYTES_EXCEEDED",
                "total_bytes",
                "maximum_total_bytes",
            ),
            (
                snapshot.entries,
                limits.maximum_entries,
                f"{prefix}_ENTRY_LIMIT_EXCEEDED",
                "entries",
                "maximum_entries",
            ),
            (
                snapshot.maximum_depth,
                limits.maximum_depth,
                (
                    "WORKSPACE_DEPTH_LIMIT_EXCEEDED"
                    if target == "workspace"
                    else "RUNTIME_CACHE_ENTRY_LIMIT_EXCEEDED"
                ),
                "maximum_depth",
                "maximum_depth",
            ),
        )
        for observed, limit, code, observed_label, limit_label in rows:
            if observed > limit:
                return ResourceEvent(
                    code=code,
                    scope="SESSION",
                    target=target,
                    phase=phase,
                    disposition=profile.disposition(code).value,
                    session_id=session_id,
                    uncertain=False,
                    observed={observed_label: observed},
                    limits={limit_label: limit},
                )
        return None

    async def _check_tree(
        self,
        session_id: str,
        target: str,
        phase: str,
    ) -> ResourceEvent | None:
        limits = (
            self.profile.workspace
            if target == "workspace"
            else self.profile.runtime_cache
        )
        root = getattr(self._paths[session_id], target)
        self._tree_checks[session_id][target] += 1
        try:
            snapshot = await scan_tree(
                root,
                limits,
                expected_root_identity=self._root_identity[session_id][target],
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            return self._uncertain(
                target=target,
                phase=phase,
                session_id=session_id,
                error=error,
            )
        self._latest_tree[session_id][target] = snapshot
        event = self._tree_limit_event(
            self.profile,
            session_id,
            target,
            phase,
            snapshot,
            limits,
        )
        if event is not None:
            self._latch(event)
        return event

    async def _disk_loop(self) -> None:
        interval = float(self.profile.disk_guard.poll_interval_seconds)
        while self._global_terminal is None:
            await asyncio.sleep(interval)
            await self._check_disk("LIVE")

    async def _tree_loop(
        self,
        session_id: str,
        target: str,
        interval: float,
    ) -> None:
        while self.event_for(session_id) is None:
            await asyncio.sleep(interval)
            await self._check_tree(
                session_id,
                target,
                "LIVE",
            )

    def _track(
        self,
        coroutine: object,
        *,
        name: str,
        session_id: str | None = None,
    ) -> None:
        task = asyncio.create_task(coroutine, name=name)  # type: ignore[arg-type]
        self._tasks.add(task)
        if session_id is not None:
            self._session_tasks[session_id].add(task)

    async def start(self) -> ResourceEvent | None:
        """Run the initial host check, then start the low-rate disk monitor."""

        if self._started or self._closed:
            raise RuntimeError("resource guard lifecycle is invalid")
        self._started = True
        event = await self._check_disk("INITIAL")
        if event is None:
            self._track(
                self._disk_loop(),
                name="pmw-resource-guard-disk",
            )
        return event

    async def activate(self, session_id: str) -> ResourceEvent | None:
        """Perform first tree checks and begin explicitly requested live scans."""

        self._require_session(session_id)
        if not self._started or self._closed or session_id in self._activated:
            raise RuntimeError("resource guard lifecycle is invalid")
        self._activated.add(session_id)
        if self._global_terminal is not None:
            return self._global_terminal
        for target in ("workspace", "cache"):
            event = await self._check_tree(
                session_id,
                target,
                "INITIAL",
            )
            if event is not None and event.disposition != Disposition.WARN.value:
                return self.event_for(session_id)

        for target, limits in (
            ("workspace", self.profile.workspace),
            ("cache", self.profile.runtime_cache),
        ):
            if limits.scan_mode == "LIVE_LATCHED":
                interval = limits.live_scan_interval_seconds
                if interval is None:
                    raise AssertionError("live scan lacks an interval")
                self._track(
                    self._tree_loop(session_id, target, float(interval)),
                    name=f"pmw-resource-guard-{session_id}-{target}",
                    session_id=session_id,
                )
        return self.event_for(session_id)

    async def wait(self, session_id: str) -> ResourceEvent:
        self._require_session(session_id)
        await self._events[session_id].wait()
        event = self.event_for(session_id)
        if event is None:
            raise AssertionError("resource signal lacks an event")
        return event

    async def finish(self, session_id: str) -> ResourceEvent | None:
        """Stop live monitors and perform one quiescent terminal measurement."""

        self._require_session(session_id)
        if session_id in self._finished:
            return self.event_for(session_id)
        if session_id not in self._activated:
            # A queued session that never reached adapter preparation cannot
            # have written workspace/cache content.  Preserve any cohort-wide
            # event without turning mass cancellation into thousands of
            # redundant empty-tree scans.
            self._finished.add(session_id)
            return self.event_for(session_id)
        tasks = tuple(self._session_tasks[session_id])
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._session_tasks[session_id].clear()
        self._tasks.difference_update(tasks)

        await self._check_disk("TERMINAL")
        for target in ("workspace", "cache"):
            await self._check_tree(
                session_id,
                target,
                "TERMINAL",
            )
        self._finished.add(session_id)
        return self.event_for(session_id)

    def evidence(self, session_id: str) -> dict[str, object]:
        """Return a fixed-shape, bounded receipt projection."""

        self._require_session(session_id)
        latest = self._latest_tree[session_id]
        return {
            "schema": RESOURCE_EVIDENCE_SCHEMA,
            "checks": {
                "disk": self._disk_checks,
                "workspace": self._tree_checks[session_id]["workspace"],
                "cache": self._tree_checks[session_id]["cache"],
            },
            "latest": {
                "disk": (
                    None
                    if self._latest_disk is None
                    else self._latest_disk.to_value()
                ),
                "workspace": (
                    None
                    if latest["workspace"] is None
                    else latest["workspace"].to_value()
                ),
                "cache": (
                    None
                    if latest["cache"] is None
                    else latest["cache"].to_value()
                ),
            },
            "terminal_event": (
                None
                if self.event_for(session_id) is None
                else self.event_for(session_id).to_value()  # type: ignore[union-attr]
            ),
            "warnings": [
                event.to_value() for event in self._warnings[session_id]
            ],
        }

    async def close(self) -> None:
        """Cancel and join every monitor; no task survives this method."""

        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        for selected in self._session_tasks.values():
            selected.clear()
