"""Validated source identities shipped with the platform."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping


CORE_LOCK_SCHEMA = "PMW_RESEARCH_CORE_LOCK_1"
_COMMIT = re.compile(r"[0-9a-f]{40}")


class SourceLockError(ValueError):
    """The installed source lock is missing or malformed."""


@dataclass(frozen=True, slots=True)
class LockedSource:
    name: str
    repository: str
    commit: str
    role: str


@dataclass(frozen=True, slots=True)
class CoreLock:
    sha256: str
    sources: Mapping[str, LockedSource]

    def source(self, name: str) -> LockedSource:
        try:
            return self.sources[name]
        except KeyError as error:
            raise SourceLockError(f"unknown locked source: {name}") from error


def load_core_lock(path: str | Path | None = None) -> CoreLock:
    selected = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parent / "locks" / "core-lock.json"
    )
    try:
        if selected.is_symlink() or not selected.is_file():
            raise SourceLockError(f"core lock is not a regular file: {selected}")
        raw = selected.read_bytes()
    except SourceLockError:
        raise
    except OSError as error:
        raise SourceLockError(f"cannot read core lock: {selected}") from error
    if not raw or len(raw) > 1_048_576:
        raise SourceLockError("core lock size is invalid")

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SourceLockError(f"duplicate core-lock key: {key}")
            result[key] = value
        return result

    def reject_float(_value: str) -> object:
        raise SourceLockError("core lock cannot contain floats")

    def reject_constant(value: str) -> object:
        raise SourceLockError(f"non-finite core-lock value: {value}")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_float=reject_float,
            parse_constant=reject_constant,
        )
    except SourceLockError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise SourceLockError("cannot parse core lock") from error
    if type(value) is not dict or set(value) != {"schema", "dependencies"}:
        raise SourceLockError("invalid core-lock envelope")
    if (
        value["schema"] != CORE_LOCK_SCHEMA
        or type(value["dependencies"]) is not dict
    ):
        raise SourceLockError("unsupported core-lock schema")

    sources: dict[str, LockedSource] = {}
    for name, raw_source in value["dependencies"].items():
        if type(name) is not str or type(raw_source) is not dict:
            raise SourceLockError("invalid source entry")
        if set(raw_source) not in (
            {"repository", "commit", "python_package"},
            {"repository", "commit", "role"},
        ):
            raise SourceLockError(f"invalid fields for locked source: {name}")
        repository = raw_source.get("repository")
        commit = raw_source.get("commit")
        role = raw_source.get("python_package", raw_source.get("role"))
        if (
            type(repository) is not str
            or not repository.startswith("https://github.com/")
            or not repository.endswith(".git")
            or type(commit) is not str
            or _COMMIT.fullmatch(commit) is None
            or type(role) is not str
            or not role
        ):
            raise SourceLockError(f"invalid identity for locked source: {name}")
        sources[name] = LockedSource(name, repository, commit, role)
    if set(sources) != {"agent-math-frontier", "persistent-mathematical-worlds"}:
        raise SourceLockError("core lock must bind the problem and PMW authorities")
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return CoreLock(
        sha256=hashlib.sha256(canonical).hexdigest(),
        sources=MappingProxyType(sources),
    )
