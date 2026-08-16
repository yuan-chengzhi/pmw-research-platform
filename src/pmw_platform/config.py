"""Local deployment registry.

World identity lives in PMW.  This module only maps a short local name to a
bare Git store and records the exact seed selected when that mapping was made.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any


REGISTRY_SCHEMA = "PMW_RESEARCH_WORLD_REGISTRY_1"
_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")


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
        if not world_ref.startswith("refs/"):
            raise ConfigError("world_ref must be a full refs/... name")
        if not seed_snapshot_ref.startswith("snapshot/sha256/"):
            raise ConfigError("seed_snapshot_ref must be a snapshot/sha256/... ref")
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
        if not Path(value["repo"]).is_absolute():
            raise ConfigError("registered world path must be absolute")
        return cls(**value)


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise ConfigError("world name must match [a-z][a-z0-9-]{0,62}")


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
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
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
        rows = self._read()
        if registration.name in rows and not replace:
            raise ConfigError(f"world already registered: {registration.name}")
        rows[registration.name] = registration
        self._write(rows)

    def _write(self, rows: dict[str, WorldRegistration]) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        for name in ("worlds", "runs", "objects", "source-cache", "archive"):
            (self.data_root / name).mkdir(exist_ok=True)
        payload = {
            "schema": REGISTRY_SCHEMA,
            "worlds": [asdict(rows[name]) for name in sorted(rows)],
        }
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        descriptor, temporary = tempfile.mkstemp(
            prefix=".registry.", suffix=".tmp", dir=self.data_root
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
