"""Local deployment registry.

World identity lives in PMW.  This module only maps a short local name to a
bare Git store and records the exact seed selected when that mapping was made.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any


REGISTRY_SCHEMA = "PMW_RESEARCH_WORLD_REGISTRY_1"
MAXIMUM_REGISTRY_BYTES = 1_048_576
_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")
_WORLD_REF = re.compile(r"refs/pmw/[A-Za-z0-9][A-Za-z0-9._/-]{0,240}")
_SNAPSHOT_REF = re.compile(r"snapshot/sha256/[0-9a-f]{64}")
_PROCESS_LOCK = threading.RLock()


class ConfigError(ValueError):
    """A local configuration is missing, ambiguous, or malformed."""


def default_data_root() -> Path:
    override = os.environ.get("PMW_RESEARCH_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / "Documents" / "pmw-research-data").resolve()


@dataclass(frozen=True, slots=True)
class WorldRegistration:
    name: str
    repo: str
    world_ref: str
    seed_snapshot_ref: str
    registered_at: str

    @classmethod
    def create(
        cls,
        *,
        name: str,
        repo: str | os.PathLike[str],
        world_ref: str,
        seed_snapshot_ref: str,
    ) -> "WorldRegistration":
        _validate_name(name)
        path = Path(repo).expanduser().resolve()
        if not path.is_dir():
            raise ConfigError(f"world repository does not exist: {path}")
        _validate_world_ref(world_ref)
        _validate_snapshot(seed_snapshot_ref)
        return cls(
            name=name,
            repo=str(path),
            world_ref=world_ref,
            seed_snapshot_ref=seed_snapshot_ref,
            registered_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )

    @classmethod
    def from_dict(cls, value: Any) -> "WorldRegistration":
        if not isinstance(value, dict) or set(value) != {
            "name",
            "repo",
            "world_ref",
            "seed_snapshot_ref",
            "registered_at",
        }:
            raise ConfigError("invalid world registration fields")
        if any(not isinstance(value[key], str) for key in value):
            raise ConfigError("world registration values must be strings")
        _validate_name(value["name"])
        repo = Path(value["repo"])
        if not repo.is_absolute() or "\x00" in value["repo"]:
            raise ConfigError("registered world path must be absolute")
        _validate_world_ref(value["world_ref"])
        _validate_snapshot(value["seed_snapshot_ref"])
        try:
            parsed = datetime.fromisoformat(
                value["registered_at"].replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ConfigError("registered_at must be an ISO-8601 timestamp") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ConfigError("registered_at must include a timezone")
        return cls(**value)


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise ConfigError("world name must match [a-z][a-z0-9-]{0,62}")


def _validate_world_ref(value: object) -> str:
    if (
        not isinstance(value, str)
        or _WORLD_REF.fullmatch(value) is None
        or "//" in value
        or "/../" in value
    ):
        raise ConfigError("world_ref must be a canonical refs/pmw/... name")
    return value


def _validate_snapshot(value: object) -> str:
    if not isinstance(value, str) or _SNAPSHOT_REF.fullmatch(value) is None:
        raise ConfigError("seed_snapshot_ref must be a canonical snapshot ref")
    return value


def _strict_json(raw: bytes) -> object:
    if not raw or len(raw) > MAXIMUM_REGISTRY_BYTES:
        raise ConfigError("registry size is invalid")

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ConfigError(f"duplicate registry key: {key}")
            result[key] = value
        return result

    def parse_float(value: str) -> float:
        selected = float(value)
        if not math.isfinite(selected):
            raise ConfigError("registry contains a non-finite number")
        return selected

    def reject_constant(value: str) -> object:
        raise ConfigError(f"registry contains a non-finite value: {value}")

    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_float=parse_float,
            parse_constant=reject_constant,
        )
    except ConfigError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ConfigError("cannot parse registry") from error


class WorldRegistry:
    """Small atomically written name registry under the runtime data root."""

    def __init__(self, data_root: str | os.PathLike[str] | None = None) -> None:
        self.data_root = (
            default_data_root()
            if data_root is None
            else Path(data_root).expanduser().resolve()
        )
        self.path = self.data_root / "registry.json"

    def _read(self) -> dict[str, WorldRegistration]:
        if not self.path.exists():
            return {}
        if self.path.is_symlink():
            raise ConfigError("registry must not be a symlink")
        try:
            metadata = self.path.stat()
            if not self.path.is_file() or metadata.st_size > MAXIMUM_REGISTRY_BYTES:
                raise ConfigError("registry is not a bounded regular file")
            raw = _strict_json(self.path.read_bytes())
        except ConfigError:
            raise
        except OSError as error:
            raise ConfigError(f"cannot read registry: {error}") from error
        if not isinstance(raw, dict) or set(raw) != {"schema", "worlds"}:
            raise ConfigError("invalid registry envelope")
        if raw["schema"] != REGISTRY_SCHEMA or not isinstance(raw["worlds"], list):
            raise ConfigError("unsupported registry schema")
        rows = [WorldRegistration.from_dict(row) for row in raw["worlds"]]
        result = {row.name: row for row in rows}
        if len(result) != len(rows):
            raise ConfigError("duplicate world name")
        return result

    def list(self) -> tuple[WorldRegistration, ...]:
        rows = self._read()
        return tuple(rows[name] for name in sorted(rows))

    def get(self, name: str) -> WorldRegistration:
        _validate_name(name)
        try:
            return self._read()[name]
        except KeyError as error:
            raise ConfigError(f"unknown world: {name}") from error

    def add(self, registration: WorldRegistration, *, replace: bool = False) -> None:
        if not isinstance(registration, WorldRegistration):
            raise ConfigError("registration must be a WorldRegistration")
        self.data_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.data_root / ".registry.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        with _PROCESS_LOCK:
            try:
                descriptor = os.open(lock_path, flags, 0o600)
            except OSError as error:
                raise ConfigError("cannot open registry lock") from error
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                rows = self._read()
                if registration.name in rows and not replace:
                    raise ConfigError(
                        f"world already registered: {registration.name}"
                    )
                rows[registration.name] = registration
                self._write(rows)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _write(self, rows: dict[str, WorldRegistration]) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        for name in ("worlds", "runs", "objects", "source-cache", "archive"):
            (self.data_root / name).mkdir(exist_ok=True)
        payload = {
            "schema": REGISTRY_SCHEMA,
            "worlds": [asdict(rows[name]) for name in sorted(rows)],
        }
        encoded = (
            json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        if len(encoded) > MAXIMUM_REGISTRY_BYTES:
            raise ConfigError("registry size limit exceeded")
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".registry.", suffix=".tmp", dir=self.data_root
            )
        except OSError as error:
            raise ConfigError("cannot create registry update") from error
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.data_root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            raise ConfigError("cannot persist registry") from error
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
