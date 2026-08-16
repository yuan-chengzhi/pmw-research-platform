"""Data-driven safety policy and bounded process-output capture.

This module deliberately does not kill processes, scan workspaces, or admit
evidence.  It gives the runtime two small primitives:

* a validated mapping from an observed condition to its policy action; and
* an output accumulator that bounds retained bytes while continuing to drain,
  count, hash, and tail the observed stream.

Keeping policy separate from enforcement prevents an accounting observation
from silently becoming a whole-session failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping


PROFILE_SCHEMA = "PMW_RESEARCH_SAFETY_PROFILE_1"
MAXIMUM_PROFILE_BYTES = 1_048_576
_PROFILE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SAFETY_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class SafetyProfileError(ValueError):
    """Raised when a policy document is malformed or incomplete."""


class Disposition(str, Enum):
    """Policy action for an observation; evidence records scope separately."""

    SESSION_STOP = "SESSION_STOP"
    JOB_STOP = "JOB_STOP"
    REJECT = "REJECT"
    WARN = "WARN"


# Every runtime-facing condition must have an explicit disposition.  Adding a
# code is intentionally a schema change: both shipped profiles then have to say
# what it means instead of inheriting a surprising default.
REQUIRED_SAFETY_CODES = frozenset({
    "ARTIFACT_INVALID",
    "ARTIFACT_TOO_LARGE",
    "CONTEXT_HARD_LIMIT",
    "CREDENTIAL_BOUNDARY_VIOLATION",
    "CROSS_SESSION_BOUNDARY_VIOLATION",
    "DISK_RESERVE_BREACHED",
    "NETWORK_BOUNDARY_VIOLATION",
    "OBSERVED_OUTPUT_SAFETY_CAP",
    "OUTPUT_READ_INVALID",
    "OUTPUT_TRUNCATED",
    "PMW_AUTHORITY_VIOLATION",
    "PROCESS_GROUP_CLEANUP_FAILED",
    "PROVIDER_ACCOUNTING_INVALID",
    "PROVIDER_REQUEST_BUDGET_EXCEEDED",
    "RUNTIME_ARTIFACT_DRIFT",
    "RUNTIME_CACHE_CHURN",
    "RUNTIME_CACHE_ENTRY_LIMIT_EXCEEDED",
    "RUNTIME_CACHE_FILE_SIZE_EXCEEDED",
    "RUNTIME_CACHE_MOUNT_BOUNDARY",
    "RUNTIME_CACHE_ROOT_DRIFT",
    "RUNTIME_CACHE_SPECIAL_FILE_REJECTED",
    "RUNTIME_CACHE_TOTAL_BYTES_EXCEEDED",
    "SANDBOX_CONTAINMENT_DRIFT",
    "SESSION_WALL_LIMIT",
    "TOOL_CANCELLED",
    "TOOL_SPAWN_FAILED",
    "TOOL_TIMEOUT",
    "VERIFIER_OUTPUT_TOO_LARGE",
    "WORKSPACE_DEPTH_LIMIT_EXCEEDED",
    "WORKSPACE_ENTRY_LIMIT_EXCEEDED",
    "WORKSPACE_FILE_SIZE_EXCEEDED",
    "WORKSPACE_HARDLINK_OBSERVED",
    "WORKSPACE_LIVE_CHURN",
    "WORKSPACE_MOUNT_BOUNDARY",
    "WORKSPACE_QUIESCENT_SCAN_FAILED",
    "WORKSPACE_ROOT_DRIFT",
    "WORKSPACE_SPECIAL_FILE_REJECTED",
    "WORKSPACE_TOTAL_BYTES_EXCEEDED",
    "WORKSPACE_UNSAFE_SYMLINK",
})


@dataclass(frozen=True)
class DiskGuard:
    """Host-wide free-space reserve used to stop real disk emergencies."""

    reserve_bytes: int
    reserve_fraction: float
    poll_interval_seconds: float

    def __post_init__(self) -> None:
        if type(self.reserve_bytes) is not int or self.reserve_bytes <= 0:
            raise ValueError("reserve_bytes must be a positive integer")
        if (
            type(self.reserve_fraction) not in {int, float}
            or not math.isfinite(float(self.reserve_fraction))
            or not 0 < self.reserve_fraction < 1
        ):
            raise ValueError("reserve_fraction must be finite and between 0 and 1")
        if (
            type(self.poll_interval_seconds) not in {int, float}
            or not math.isfinite(float(self.poll_interval_seconds))
            or self.poll_interval_seconds <= 0
        ):
            raise ValueError("poll_interval_seconds must be positive and finite")

    def required_free_bytes(self, filesystem_total_bytes: int) -> int:
        if type(filesystem_total_bytes) is not int or filesystem_total_bytes <= 0:
            raise ValueError("filesystem_total_bytes must be a positive integer")
        return max(
            self.reserve_bytes,
            math.ceil(filesystem_total_bytes * self.reserve_fraction),
        )


@dataclass(frozen=True)
class TreeLimits:
    """Accounting bounds for a workspace or disposable runtime cache."""

    maximum_total_bytes: int
    maximum_entries: int
    maximum_file_bytes: int | None
    maximum_depth: int
    scan_mode: str
    live_scan_interval_seconds: float | None

    def __post_init__(self) -> None:
        for label in ("maximum_total_bytes", "maximum_entries", "maximum_depth"):
            value = getattr(self, label)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if self.maximum_file_bytes is not None and (
            type(self.maximum_file_bytes) is not int
            or not 0 < self.maximum_file_bytes <= self.maximum_total_bytes
        ):
            raise ValueError("maximum_file_bytes is invalid")
        if self.scan_mode == "QUIESCENT":
            if self.live_scan_interval_seconds is not None:
                raise ValueError("quiescent scan cannot have a live interval")
        elif self.scan_mode == "LIVE_LATCHED":
            if (
                type(self.live_scan_interval_seconds) not in {int, float}
                or not math.isfinite(float(self.live_scan_interval_seconds))
                or self.live_scan_interval_seconds <= 0
            ):
                raise ValueError("live scan interval must be positive and finite")
        else:
            raise ValueError("scan_mode is invalid")

    def legacy_file_limit_disposition(
        self,
        size: int,
        *,
        profile: "SafetyProfile",
        code: str,
    ) -> Disposition | None:
        """Interpret retained legacy metadata without enforcing it.

        The generic :class:`ResourceGuard` deliberately does not call this
        helper.  It exists only for inspecting historical profile intent.
        """

        if type(size) is not int or size < 0:
            raise ValueError("size must be a non-negative integer")
        if self.maximum_file_bytes is None or size <= self.maximum_file_bytes:
            return None
        return profile.disposition(code)


@dataclass(frozen=True)
class CaptureLimits:
    """Per-stream retention and execution-safety bounds."""

    maximum_retained_bytes: int
    maximum_observed_bytes: int
    tail_bytes: int

    def __post_init__(self) -> None:
        for label in (
            "maximum_retained_bytes",
            "maximum_observed_bytes",
            "tail_bytes",
        ):
            value = getattr(self, label)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if self.maximum_retained_bytes > self.maximum_observed_bytes:
            raise ValueError("retained cap exceeds observed cap")
        if self.tail_bytes > self.maximum_retained_bytes:
            raise ValueError("tail exceeds retained cap")


@dataclass(frozen=True)
class CaptureAppendOutcome:
    """Result of observing one output chunk."""

    observed_bytes_added: int
    retained_bytes_added: int
    truncated: bool
    safety_cap_crossed: bool
    disposition: Disposition | None


@dataclass(frozen=True)
class CaptureSnapshot:
    """Immutable state of one captured stream.

    ``retained_bytes`` accounts for the bounded prefix selected for retention.
    ``retained`` is that prefix only when ``retained_content_in_snapshot`` is
    true; streaming callers can externalize it and receive ``None`` here.
    """

    observed_bytes: int
    retained_bytes: int
    retained: bytes | None
    tail: bytes
    observed_sha256: str
    truncated: bool
    observed_safety_cap_exceeded: bool
    terminal_disposition: Disposition | None

    @property
    def retained_content_in_snapshot(self) -> bool:
        """Whether ``retained`` contains the accounted prefix bytes."""

        return self.retained is not None


@dataclass(frozen=True)
class SafetyProfile:
    """Validated, immutable safety policy loaded from JSON.

    ``legacy_captures`` preserves frozen profile identity.  Generic runtime
    output limits belong to each backend's launch identity because command
    streams and Pi RPC frames are not interchangeable.
    """

    name: str
    sha256: str
    dispositions: Mapping[str, Disposition]
    disk_guard: DiskGuard
    workspace: TreeLimits
    runtime_cache: TreeLimits
    legacy_captures: Mapping[str, CaptureLimits]

    def __post_init__(self) -> None:
        if type(self.name) is not str or _PROFILE_NAME.fullmatch(self.name) is None:
            raise ValueError("profile name is invalid")
        if type(self.sha256) is not str or re.fullmatch(
            r"[0-9a-f]{64}", self.sha256
        ) is None:
            raise ValueError("profile digest is invalid")
        if set(self.dispositions) != REQUIRED_SAFETY_CODES or any(
            not isinstance(value, Disposition)
            for value in self.dispositions.values()
        ):
            raise ValueError("profile dispositions are incomplete")
        if (
            self.dispositions["OBSERVED_OUTPUT_SAFETY_CAP"]
            is not Disposition.JOB_STOP
        ):
            raise ValueError("observed output safety cap must be JOB_STOP")
        if not isinstance(self.disk_guard, DiskGuard):
            raise TypeError("disk_guard must be DiskGuard")
        if not isinstance(self.workspace, TreeLimits) or not isinstance(
            self.runtime_cache, TreeLimits
        ):
            raise TypeError("tree limits are invalid")
        if set(self.legacy_captures) != {"bash", "long_job"} or any(
            not isinstance(value, CaptureLimits)
            for value in self.legacy_captures.values()
        ):
            raise ValueError("capture limits are incomplete")

    def disposition(self, code: str) -> Disposition:
        if type(code) is not str or _SAFETY_CODE.fullmatch(code) is None:
            raise KeyError(code)
        try:
            return self.dispositions[code]
        except KeyError as exc:
            raise KeyError(f"unclassified safety code: {code}") from exc

    def legacy_capture_limits(self, kind: str) -> CaptureLimits:
        """Inspect historical campaign capture metadata; not runtime policy."""

        try:
            return self.legacy_captures[kind]
        except KeyError as exc:
            raise KeyError(f"unknown capture kind: {kind}") from exc

    def new_legacy_capture(self, kind: str) -> "BoundedCaptureAccumulator":
        """Build a capture matching legacy metadata for audit/replay tooling."""

        return BoundedCaptureAccumulator(
            self.legacy_capture_limits(kind),
            observed_cap_disposition=self.disposition(
                "OBSERVED_OUTPUT_SAFETY_CAP"
            ),
        )

    def legacy_workspace_file_limit_disposition(
        self, size: int
    ) -> Disposition | None:
        """Inspect the historical workspace file-cap policy metadata."""

        return self.workspace.legacy_file_limit_disposition(
            size,
            profile=self,
            code="WORKSPACE_FILE_SIZE_EXCEEDED",
        )


class BoundedCaptureAccumulator:
    """Drain output without allowing retained evidence to grow unbounded.

    Crossing the retained cap is deliberately non-terminal.  The accumulator
    keeps counting and hashing every chunk and maintains a bounded tail.  It
    only signals the profile's job-local disposition after the much larger
    observed-output safety cap is crossed; the caller owns process control.
    With ``retain_content=False`` it accounts for the same prefix but leaves
    storage to the caller, avoiding a duplicate in-memory copy.
    """

    def __init__(
        self,
        limits: CaptureLimits,
        *,
        observed_cap_disposition: Disposition = Disposition.JOB_STOP,
        retain_content: bool = True,
    ) -> None:
        if not isinstance(limits, CaptureLimits):
            raise TypeError("limits must be CaptureLimits")
        if observed_cap_disposition is not Disposition.JOB_STOP:
            raise ValueError("observed output safety cap must be JOB_STOP")
        if type(retain_content) is not bool:
            raise TypeError("retain_content must be bool")
        self._limits = limits
        self._observed_cap_disposition = observed_cap_disposition
        self._observed_bytes = 0
        self._retained_bytes = 0
        self._retained = bytearray() if retain_content else None
        self._tail = bytearray()
        self._digest = hashlib.sha256()
        self._safety_cap_exceeded = False
        self._finalized = False

    @property
    def limits(self) -> CaptureLimits:
        return self._limits

    def append(self, chunk: bytes | bytearray | memoryview) -> CaptureAppendOutcome:
        if self._finalized:
            raise RuntimeError("capture is finalized")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("chunk must be bytes-like")
        selected = bytes(chunk)
        before_retained = self._retained_bytes
        self._observed_bytes += len(selected)
        self._digest.update(selected)

        room = self._limits.maximum_retained_bytes - self._retained_bytes
        if room > 0:
            retained_bytes_added = min(room, len(selected))
            if self._retained is not None:
                self._retained.extend(selected[:retained_bytes_added])
            self._retained_bytes += retained_bytes_added

        if self._limits.tail_bytes:
            if len(selected) >= self._limits.tail_bytes:
                self._tail[:] = selected[-self._limits.tail_bytes :]
            else:
                self._tail.extend(selected)
                overflow = len(self._tail) - self._limits.tail_bytes
                if overflow > 0:
                    del self._tail[:overflow]

        was_exceeded = self._safety_cap_exceeded
        if self._observed_bytes > self._limits.maximum_observed_bytes:
            self._safety_cap_exceeded = True
        disposition = (
            self._observed_cap_disposition
            if self._safety_cap_exceeded
            else None
        )
        return CaptureAppendOutcome(
            observed_bytes_added=len(selected),
            retained_bytes_added=self._retained_bytes - before_retained,
            truncated=self._observed_bytes > self._retained_bytes,
            safety_cap_crossed=self._safety_cap_exceeded and not was_exceeded,
            disposition=disposition,
        )

    def snapshot(self) -> CaptureSnapshot:
        return CaptureSnapshot(
            observed_bytes=self._observed_bytes,
            retained_bytes=self._retained_bytes,
            retained=(
                bytes(self._retained) if self._retained is not None else None
            ),
            tail=bytes(self._tail),
            observed_sha256=self._digest.hexdigest(),
            truncated=self._observed_bytes > self._retained_bytes,
            observed_safety_cap_exceeded=self._safety_cap_exceeded,
            terminal_disposition=(
                self._observed_cap_disposition
                if self._safety_cap_exceeded
                else None
            ),
        )

    def finalize(self) -> CaptureSnapshot:
        self._finalized = True
        return self.snapshot()


def _strict_json(path: Path) -> object:
    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        selected: dict[str, object] = {}
        for key, value in pairs:
            if key in selected:
                raise SafetyProfileError(f"duplicate JSON key: {key}")
            selected[key] = value
        return selected

    def reject_constant(value: str) -> object:
        raise SafetyProfileError(f"non-finite JSON value: {value}")

    try:
        if path.stat().st_size > MAXIMUM_PROFILE_BYTES:
            raise SafetyProfileError("safety profile is too large")
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except SafetyProfileError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SafetyProfileError(f"cannot load safety profile: {path}") from exc


def _require_exact_keys(
    value: object,
    expected: set[str] | frozenset[str],
    *,
    label: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(expected):
        raise SafetyProfileError(f"{label} keys are invalid")
    return value


def _positive_int(value: object, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise SafetyProfileError(f"{label} must be a positive integer")
    return value


def _positive_number(value: object, *, label: str) -> float:
    if (
        type(value) not in {int, float}
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise SafetyProfileError(f"{label} must be a positive finite number")
    return float(value)


def _parse_tree(value: object, *, label: str) -> TreeLimits:
    row = _require_exact_keys(
        value,
        {
            "maximum_depth",
            "maximum_entries",
            "maximum_file_bytes",
            "maximum_total_bytes",
            "scan_mode",
            "live_scan_interval_seconds",
        },
        label=label,
    )
    maximum_total_bytes = _positive_int(
        row["maximum_total_bytes"], label=f"{label}.maximum_total_bytes"
    )
    raw_file_limit = row["maximum_file_bytes"]
    if raw_file_limit is None:
        maximum_file_bytes = None
    else:
        maximum_file_bytes = _positive_int(
            raw_file_limit, label=f"{label}.maximum_file_bytes"
        )
        if maximum_file_bytes > maximum_total_bytes:
            raise SafetyProfileError(f"{label} file limit exceeds total limit")
    scan_mode = row["scan_mode"]
    if scan_mode not in {"QUIESCENT", "LIVE_LATCHED"}:
        raise SafetyProfileError(f"{label}.scan_mode is invalid")
    raw_interval = row["live_scan_interval_seconds"]
    if scan_mode == "QUIESCENT":
        if raw_interval is not None:
            raise SafetyProfileError(
                f"{label} quiescent scans cannot have a live interval"
            )
        interval = None
    else:
        interval = _positive_number(
            raw_interval, label=f"{label}.live_scan_interval_seconds"
        )
    return TreeLimits(
        maximum_total_bytes=maximum_total_bytes,
        maximum_entries=_positive_int(
            row["maximum_entries"], label=f"{label}.maximum_entries"
        ),
        maximum_file_bytes=maximum_file_bytes,
        maximum_depth=_positive_int(
            row["maximum_depth"], label=f"{label}.maximum_depth"
        ),
        scan_mode=scan_mode,
        live_scan_interval_seconds=interval,
    )


def validate_profile(value: object) -> SafetyProfile:
    """Validate a decoded JSON value and return an immutable profile."""

    root = _require_exact_keys(
        value,
        {
            "captures",
            "disk_guard",
            "dispositions",
            "name",
            "runtime_cache",
            "schema",
            "workspace",
        },
        label="profile",
    )
    if root["schema"] != PROFILE_SCHEMA:
        raise SafetyProfileError("unsupported safety profile schema")
    name = root["name"]
    if type(name) is not str or _PROFILE_NAME.fullmatch(name) is None:
        raise SafetyProfileError("profile name is invalid")

    raw_dispositions = _require_exact_keys(
        root["dispositions"], REQUIRED_SAFETY_CODES, label="dispositions"
    )
    dispositions: dict[str, Disposition] = {}
    for code, raw_disposition in raw_dispositions.items():
        if _SAFETY_CODE.fullmatch(code) is None:
            raise SafetyProfileError(f"invalid safety code: {code}")
        try:
            dispositions[code] = Disposition(raw_disposition)
        except (TypeError, ValueError) as exc:
            raise SafetyProfileError(
                f"invalid disposition for {code}"
            ) from exc
    if dispositions["OBSERVED_OUTPUT_SAFETY_CAP"] is not Disposition.JOB_STOP:
        raise SafetyProfileError(
            "observed output safety cap must be JOB_STOP"
        )

    raw_disk = _require_exact_keys(
        root["disk_guard"],
        {"poll_interval_seconds", "reserve_bytes", "reserve_fraction"},
        label="disk_guard",
    )
    reserve_fraction = _positive_number(
        raw_disk["reserve_fraction"], label="disk_guard.reserve_fraction"
    )
    if reserve_fraction >= 1:
        raise SafetyProfileError("disk_guard.reserve_fraction must be below 1")
    disk_guard = DiskGuard(
        reserve_bytes=_positive_int(
            raw_disk["reserve_bytes"], label="disk_guard.reserve_bytes"
        ),
        reserve_fraction=reserve_fraction,
        poll_interval_seconds=_positive_number(
            raw_disk["poll_interval_seconds"],
            label="disk_guard.poll_interval_seconds",
        ),
    )

    raw_captures = _require_exact_keys(
        root["captures"], {"bash", "long_job"}, label="captures"
    )
    captures: dict[str, CaptureLimits] = {}
    for kind, raw_capture in raw_captures.items():
        row = _require_exact_keys(
            raw_capture,
            {"maximum_observed_bytes", "maximum_retained_bytes", "tail_bytes"},
            label=f"captures.{kind}",
        )
        limits = CaptureLimits(
            maximum_retained_bytes=_positive_int(
                row["maximum_retained_bytes"],
                label=f"captures.{kind}.maximum_retained_bytes",
            ),
            maximum_observed_bytes=_positive_int(
                row["maximum_observed_bytes"],
                label=f"captures.{kind}.maximum_observed_bytes",
            ),
            tail_bytes=_positive_int(
                row["tail_bytes"], label=f"captures.{kind}.tail_bytes"
            ),
        )
        if limits.maximum_retained_bytes > limits.maximum_observed_bytes:
            raise SafetyProfileError(
                f"captures.{kind} retained cap exceeds observed cap"
            )
        if limits.tail_bytes > limits.maximum_retained_bytes:
            raise SafetyProfileError(
                f"captures.{kind} tail exceeds retained cap"
            )
        captures[kind] = limits

    semantic_sha256 = hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return SafetyProfile(
        name=name,
        sha256=semantic_sha256,
        dispositions=MappingProxyType(dispositions),
        disk_guard=disk_guard,
        workspace=_parse_tree(root["workspace"], label="workspace"),
        runtime_cache=_parse_tree(root["runtime_cache"], label="runtime_cache"),
        legacy_captures=MappingProxyType(captures),
    )


def load_profile(path: str | Path) -> SafetyProfile:
    """Load one strict JSON profile from an explicit filesystem path."""

    selected = Path(path)
    if selected.is_symlink() or not selected.is_file():
        raise SafetyProfileError(f"profile is not a regular file: {selected}")
    return validate_profile(_strict_json(selected))


def load_named_profile(
    name: str,
    *,
    profiles_dir: str | Path | None = None,
) -> SafetyProfile:
    """Load a shipped profile by name without permitting path traversal."""

    if type(name) is not str or _PROFILE_NAME.fullmatch(name) is None:
        raise SafetyProfileError("profile name is invalid")
    root = (
        Path(profiles_dir)
        if profiles_dir is not None
        else Path(__file__).resolve().parents[1] / "profiles"
    )
    profile = load_profile(root / f"{name}.json")
    if profile.name != name:
        raise SafetyProfileError("profile name does not match requested name")
    return profile
