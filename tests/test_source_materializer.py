from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from types import MappingProxyType

import pytest

from pmw_platform.source_lock import CoreLock, LockedSource
from pmw_platform.source_materializer import (
    MATERIALIZATION_SCHEMA,
    SourceMaterializer,
    SourceMaterializerError,
)


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "plain.txt").write_text("locked\n")
    nested = repo / "bin"
    nested.mkdir()
    executable = nested / "tool"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    _git(repo, "add", "plain.txt", "bin/tool")
    _git(repo, "commit", "-qm", "locked")
    return repo, _git(repo, "rev-parse", "HEAD")


def _tree_digest(repository: Path, commit: str) -> str:
    raw_entries = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", "-r", "-z", commit],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    files: list[dict[str, object]] = []
    for record in raw_entries.split(b"\x00"):
        if not record:
            continue
        identity, raw_path = record.split(b"\t", 1)
        raw_mode, _kind, raw_object = identity.split(b" ", 2)
        blob = subprocess.run(
            ["git", "-C", str(repository), "cat-file", "blob", raw_object],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
        files.append(
            {
                "bytes": len(blob),
                "git_blob": raw_object.decode(),
                "git_mode": raw_mode.decode(),
                "path": raw_path.decode(),
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
        )
    files.sort(key=lambda row: row["path"].encode("utf-8"))
    content = {
        "files": files,
        "git_tree": _git(repository, "rev-parse", f"{commit}^{{tree}}"),
    }
    canonical = json.dumps(
        content,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _lock(repository: Path, commit: str) -> CoreLock:
    source = LockedSource(
        "agent-math-frontier",
        "https://github.com/example/agent-math-frontier.git",
        commit,
        "problem-and-verifier-authority",
        _tree_digest(repository, commit),
    )
    return CoreLock("a" * 64, MappingProxyType({source.name: source}))


def test_materializes_locked_commit_without_reading_head_or_worktree(tmp_path: Path) -> None:
    repo, commit = _repository(tmp_path)
    (repo / "plain.txt").write_text("different HEAD\n")
    _git(repo, "add", "plain.txt")
    _git(repo, "commit", "-qm", "later head")
    (repo / "plain.txt").write_text("dirty worktree\n")
    (repo / "untracked.txt").write_text("not in commit\n")
    _git(repo, "add", "plain.txt")

    materializer = SourceMaterializer(
        tmp_path / "data", core_lock=_lock(repo, commit)
    )
    result = materializer.ensure(
        "agent-math-frontier", local_repository=repo
    )

    assert result.tree_path == (
        tmp_path / "data" / "source-cache" / "agent-math-frontier" / commit / "tree"
    )
    assert (result.tree_path / "plain.txt").read_text() == "locked\n"
    assert not (result.tree_path / "untracked.txt").exists()
    assert stat.S_IMODE((result.tree_path / "plain.txt").stat().st_mode) == 0o444
    assert stat.S_IMODE((result.tree_path / "bin" / "tool").stat().st_mode) == 0o555
    assert stat.S_IMODE(result.tree_path.stat().st_mode) == 0o555

    manifest_raw = result.manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    assert manifest["schema"] == MATERIALIZATION_SCHEMA
    assert manifest["source"] == {
        "commit": commit,
        "git_tree": _git(repo, "rev-parse", f"{commit}^{{tree}}"),
        "name": "agent-math-frontier",
        "repository": "https://github.com/example/agent-math-frontier.git",
    }
    assert [row["path"] for row in manifest["files"]] == ["bin/tool", "plain.txt"]
    assert manifest["summary"]["file_count"] == 2
    assert manifest["summary"]["total_bytes"] == len(b"#!/bin/sh\nexit 0\nlocked\n")
    assert result.manifest_sha256 == hashlib.sha256(manifest_raw).hexdigest()


def test_ensure_is_idempotent_and_cached_audit_does_not_need_git(tmp_path: Path) -> None:
    repo, commit = _repository(tmp_path)
    materializer = SourceMaterializer(
        tmp_path / "data", core_lock=_lock(repo, commit)
    )
    first = materializer.ensure("agent-math-frontier", local_repository=repo)
    manifest_before = first.manifest_path.read_bytes()

    second = materializer.ensure(
        "agent-math-frontier", local_repository=tmp_path / "does-not-exist"
    )
    third = materializer.audit("agent-math-frontier")

    assert second == first
    assert third == first
    assert third.manifest_path.read_bytes() == manifest_before
    assert not list(first.root.parent.glob(f".{commit}.*"))


def test_read_only_audit_of_missing_entry_creates_nothing(tmp_path: Path) -> None:
    repo, commit = _repository(tmp_path)
    data_root = tmp_path / "absent"
    materializer = SourceMaterializer(data_root, core_lock=_lock(repo, commit))

    with pytest.raises(SourceMaterializerError) as raised:
        materializer.audit("agent-math-frontier")

    assert raised.value.code == "SOURCE_NOT_MATERIALIZED"
    assert not data_root.exists()


def test_existing_tampered_cache_fails_closed_and_is_not_replaced(tmp_path: Path) -> None:
    repo, commit = _repository(tmp_path)
    materializer = SourceMaterializer(
        tmp_path / "data", core_lock=_lock(repo, commit)
    )
    result = materializer.ensure("agent-math-frontier", local_repository=repo)
    target = result.tree_path / "plain.txt"
    result.root.chmod(0o755)
    result.tree_path.chmod(0o755)
    target.chmod(0o644)
    target.write_text("tampered\n")
    target.chmod(0o444)
    result.tree_path.chmod(0o555)
    result.root.chmod(0o555)

    with pytest.raises(SourceMaterializerError) as raised:
        materializer.ensure("agent-math-frontier", local_repository=repo)

    assert raised.value.code == "SOURCE_CACHE_CONFLICT"
    assert target.read_text() == "tampered\n"


def test_symlink_in_locked_git_tree_is_rejected_without_publication(tmp_path: Path) -> None:
    repo, _commit = _repository(tmp_path)
    os.symlink("plain.txt", repo / "linked")
    _git(repo, "add", "linked")
    _git(repo, "commit", "-qm", "symlink")
    commit = _git(repo, "rev-parse", "HEAD")
    materializer = SourceMaterializer(
        tmp_path / "data", core_lock=_lock(repo, commit)
    )

    with pytest.raises(SourceMaterializerError) as raised:
        materializer.ensure("agent-math-frontier", local_repository=repo)

    assert raised.value.code == "UNSUPPORTED_GIT_ENTRY"
    target = tmp_path / "data" / "source-cache" / "agent-math-frontier" / commit
    assert not target.exists()


def test_audit_rejects_extra_entry_and_symlink_envelope(tmp_path: Path) -> None:
    repo, commit = _repository(tmp_path)
    materializer = SourceMaterializer(
        tmp_path / "data", core_lock=_lock(repo, commit)
    )
    result = materializer.ensure("agent-math-frontier", local_repository=repo)
    result.root.chmod(0o755)
    result.tree_path.chmod(0o755)
    (result.tree_path / "extra").write_text("extra")
    result.tree_path.chmod(0o555)
    result.root.chmod(0o555)

    with pytest.raises(SourceMaterializerError) as raised:
        materializer.audit("agent-math-frontier")
    assert raised.value.code == "SOURCE_CACHE_CONFLICT"

    other_root = tmp_path / "other-data"
    target = other_root / "source-cache" / "agent-math-frontier" / commit
    target.parent.mkdir(parents=True)
    os.symlink(result.root, target)
    other = SourceMaterializer(other_root, core_lock=_lock(repo, commit))
    with pytest.raises(SourceMaterializerError) as symlinked:
        other.audit("agent-math-frontier")
    assert symlinked.value.code == "SOURCE_CACHE_CONFLICT"


def test_source_cache_symlink_cannot_escape_data_root(tmp_path: Path) -> None:
    repo, commit = _repository(tmp_path)
    data_root = tmp_path / "data"
    external = tmp_path / "external"
    data_root.mkdir()
    external.mkdir()
    os.symlink(external, data_root / "source-cache")
    materializer = SourceMaterializer(data_root, core_lock=_lock(repo, commit))

    with pytest.raises(SourceMaterializerError) as audited:
        materializer.audit("agent-math-frontier")
    assert audited.value.code == "SOURCE_CACHE_CONFLICT"

    with pytest.raises(SourceMaterializerError) as raised:
        materializer.ensure("agent-math-frontier", local_repository=repo)

    assert raised.value.code == "UNSAFE_SOURCE_CACHE_PATH"
    assert list(external.iterdir()) == []


def test_wrong_lock_digest_prevents_publication(tmp_path: Path) -> None:
    repo, commit = _repository(tmp_path)
    source = LockedSource(
        "agent-math-frontier",
        "https://github.com/example/agent-math-frontier.git",
        commit,
        "problem-and-verifier-authority",
        "0" * 64,
    )
    core_lock = CoreLock("a" * 64, MappingProxyType({source.name: source}))
    materializer = SourceMaterializer(tmp_path / "data", core_lock=core_lock)

    with pytest.raises(SourceMaterializerError) as raised:
        materializer.ensure("agent-math-frontier", local_repository=repo)

    assert raised.value.code == "LOCKED_TREE_DIGEST_MISMATCH"
    target = tmp_path / "data" / "source-cache" / source.name / commit
    assert not target.exists()


def test_rewritten_tree_and_self_signed_manifest_cannot_replace_lock(
    tmp_path: Path,
) -> None:
    repo, commit = _repository(tmp_path)
    materializer = SourceMaterializer(
        tmp_path / "data", core_lock=_lock(repo, commit)
    )
    result = materializer.ensure("agent-math-frontier", local_repository=repo)
    manifest = json.loads(result.manifest_path.read_bytes())
    replacement = b"coordinated manifest and tree rewrite\n"

    result.root.chmod(0o755)
    result.tree_path.chmod(0o755)
    target = result.tree_path / "plain.txt"
    target.chmod(0o644)
    target.write_bytes(replacement)
    target.chmod(0o444)
    for row in manifest["files"]:
        if row["path"] == "plain.txt":
            row["bytes"] = len(replacement)
            row["sha256"] = hashlib.sha256(replacement).hexdigest()
    manifest["summary"]["total_bytes"] = sum(
        row["bytes"] for row in manifest["files"]
    )
    content = {
        "files": manifest["files"],
        "git_tree": manifest["source"]["git_tree"],
    }
    manifest["summary"]["tree_sha256"] = hashlib.sha256(
        json.dumps(
            content,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    encoded = (
        json.dumps(
            manifest,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    result.manifest_path.chmod(0o644)
    result.manifest_path.write_bytes(encoded)
    result.manifest_path.chmod(0o444)
    result.tree_path.chmod(0o555)
    result.root.chmod(0o555)

    with pytest.raises(SourceMaterializerError) as raised:
        materializer.audit("agent-math-frontier")

    assert raised.value.code == "LOCKED_TREE_DIGEST_MISMATCH"
