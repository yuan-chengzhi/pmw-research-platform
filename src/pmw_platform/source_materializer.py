"""Deterministic, local-only materialization of sources pinned by core-lock.

The cache entry is an immutable envelope::

    source-cache/<name>/<commit>/
      manifest.json
      tree/                 # the exact Git tree, with no platform metadata

Keeping the manifest outside ``tree/`` lets consumers use an exact checkout
while the envelope itself can be published with one atomic directory rename.
Only Git objects are read: HEAD, the index, and worktree contents are never
consulted.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import threading
from typing import NoReturn

from .source_lock import CoreLock, LockedSource, load_core_lock


MATERIALIZATION_SCHEMA = "PMW_LOCKED_SOURCE_MATERIALIZATION_1"
MANIFEST_NAME = "manifest.json"
TREE_NAME = "tree"
MAXIMUM_GIT_METADATA_BYTES = 128 * 1024 * 1024
MAXIMUM_MANIFEST_BYTES = 128 * 1024 * 1024

_SOURCE_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_PROCESS_LOCK = threading.RLock()


class SourceMaterializerError(ValueError):
    """A locked source could not be safely materialized or audited."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _fail(code: str, detail: str = "") -> NoReturn:
    raise SourceMaterializerError(code, detail)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _stable_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _verify_stable_path(
    path: Path,
    metadata: os.stat_result,
    *,
    error_code: str,
) -> None:
    try:
        named_metadata = path.lstat()
    except OSError as error:
        raise SourceMaterializerError(error_code, str(path)) from error
    if (
        stat.S_ISLNK(named_metadata.st_mode)
        or _stable_metadata(named_metadata) != _stable_metadata(metadata)
    ):
        _fail(error_code, str(path))


def _hash_regular_file(path: Path) -> tuple[str, int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SourceMaterializerError("SOURCE_FILE_UNAVAILABLE", str(path)) from error
    digest = hashlib.sha256()
    count = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            _fail("UNSAFE_SOURCE_ENTRY", str(path))
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
        after = os.fstat(descriptor)
        if (
            _stable_metadata(before) != _stable_metadata(after)
            or count != before.st_size
        ):
            _fail("SOURCE_FILE_UNSTABLE", str(path))
    finally:
        os.close(descriptor)
    _verify_stable_path(path, after, error_code="SOURCE_FILE_UNSTABLE")
    return digest.hexdigest(), count, after


def _read_bounded_stable_manifest(
    path: Path,
) -> tuple[bytes, str, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SourceMaterializerError("SOURCE_MANIFEST_UNSTABLE", str(path)) from error
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    count = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            _fail("SOURCE_MANIFEST_UNSTABLE", str(path))
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAXIMUM_MANIFEST_BYTES + 1 - count),
            )
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            count += len(chunk)
            if count > MAXIMUM_MANIFEST_BYTES:
                _fail("SOURCE_MANIFEST_INVALID", "size")
        after = os.fstat(descriptor)
        if (
            _stable_metadata(before) != _stable_metadata(after)
            or count != before.st_size
        ):
            _fail("SOURCE_MANIFEST_UNSTABLE", str(path))
    finally:
        os.close(descriptor)
    _verify_stable_path(path, after, error_code="SOURCE_MANIFEST_UNSTABLE")
    return b"".join(chunks), digest.hexdigest(), after


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return environment


def _run_git(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    git_dir: Path | None = None,
) -> bytes:
    command = ["git", "--no-pager", "-c", "protocol.allow=never"]
    if git_dir is not None:
        command.append(f"--git-dir={git_dir}")
    command.extend(arguments)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except (OSError, ValueError) as error:
        raise SourceMaterializerError("GIT_UNAVAILABLE", str(error)) from error
    if completed.returncode != 0:
        detail = completed.stderr[:2_000].decode("utf-8", errors="replace").strip()
        _fail("GIT_OBJECT_READ_FAILED", detail)
    if len(completed.stdout) > MAXIMUM_GIT_METADATA_BYTES:
        _fail("GIT_METADATA_TOO_LARGE")
    return completed.stdout


def _resolve_git_dir(repository: str | os.PathLike[str]) -> Path:
    supplied = Path(repository).expanduser()
    try:
        if supplied.is_symlink() or not supplied.is_dir():
            _fail("LOCAL_GIT_REPOSITORY_INVALID", str(supplied))
        selected = supplied.resolve(strict=True)
    except SourceMaterializerError:
        raise
    except OSError as error:
        raise SourceMaterializerError(
            "LOCAL_GIT_REPOSITORY_INVALID", str(supplied)
        ) from error
    raw = _run_git(["rev-parse", "--absolute-git-dir"], cwd=selected)
    try:
        value = raw.decode("utf-8", errors="strict").strip()
        git_dir = Path(value)
        if not git_dir.is_absolute() or git_dir.is_symlink():
            _fail("LOCAL_GIT_REPOSITORY_INVALID", value)
        resolved = git_dir.resolve(strict=True)
    except SourceMaterializerError:
        raise
    except (OSError, UnicodeError) as error:
        raise SourceMaterializerError(
            "LOCAL_GIT_REPOSITORY_INVALID", str(supplied)
        ) from error
    if not resolved.is_dir():
        _fail("LOCAL_GIT_REPOSITORY_INVALID", str(resolved))
    return resolved


@dataclass(frozen=True, slots=True)
class _GitEntry:
    path: str
    mode: str
    object_id: str


def _safe_git_path(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise SourceMaterializerError("UNSAFE_GIT_PATH", "non-UTF-8 path") from error
    candidate = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or candidate.is_absolute()
        or any(part in ("", ".", "..") for part in candidate.parts)
        or candidate.as_posix() != value
        or "\x00" in value
    ):
        _fail("UNSAFE_GIT_PATH", value)
    return value


def _read_git_tree(git_dir: Path, source: LockedSource) -> tuple[str, tuple[_GitEntry, ...]]:
    kind = _run_git(["cat-file", "-t", source.commit], git_dir=git_dir)
    if kind != b"commit\n":
        _fail("LOCKED_OBJECT_NOT_COMMIT", source.commit)
    raw_tree = _run_git(["rev-parse", f"{source.commit}^{{tree}}"], git_dir=git_dir)
    try:
        tree = raw_tree.decode("ascii", errors="strict").strip()
    except UnicodeError as error:
        raise SourceMaterializerError("GIT_TREE_ID_INVALID") from error
    if _OBJECT_ID.fullmatch(tree) is None:
        _fail("GIT_TREE_ID_INVALID", tree)

    raw_entries = _run_git(
        ["ls-tree", "-r", "-z", "--full-tree", source.commit],
        git_dir=git_dir,
    )
    entries: list[_GitEntry] = []
    seen: set[str] = set()
    for record in raw_entries.split(b"\x00"):
        if not record:
            continue
        try:
            identity, raw_path = record.split(b"\t", 1)
            raw_mode, raw_kind, raw_object = identity.split(b" ", 2)
            mode = raw_mode.decode("ascii", errors="strict")
            object_kind = raw_kind.decode("ascii", errors="strict")
            object_id = raw_object.decode("ascii", errors="strict")
        except (ValueError, UnicodeError) as error:
            raise SourceMaterializerError("GIT_TREE_RECORD_INVALID") from error
        path = _safe_git_path(raw_path)
        if (
            mode not in ("100644", "100755")
            or object_kind != "blob"
            or _OBJECT_ID.fullmatch(object_id) is None
        ):
            _fail("UNSUPPORTED_GIT_ENTRY", f"{mode} {object_kind} {path}")
        if path in seen:
            _fail("GIT_TREE_RECORD_INVALID", f"duplicate path: {path}")
        seen.add(path)
        entries.append(_GitEntry(path, mode, object_id))
    entries.sort(key=lambda entry: entry.path.encode("utf-8"))
    return tree, tuple(entries)


def _make_parents(
    root: Path,
    relative: PurePosixPath,
    known_directories: set[str],
) -> None:
    current = root
    logical = PurePosixPath()
    for part in relative.parts[:-1]:
        current = current / part
        logical = logical / part
        key = logical.as_posix()
        if key in known_directories:
            try:
                metadata = current.lstat()
            except OSError as error:
                raise SourceMaterializerError(
                    "SOURCE_PATH_COLLISION", str(current)
                ) from error
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                _fail("SOURCE_PATH_COLLISION", str(current))
            continue
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            # A path that was not previously created but already resolves is
            # a case/Unicode-normalization collision on this filesystem.
            _fail("SOURCE_PATH_COLLISION", str(current))
        except OSError as error:
            raise SourceMaterializerError("SOURCE_TREE_WRITE_FAILED", str(current)) from error
        known_directories.add(key)


def _write_blob(git_dir: Path, entry: _GitEntry, destination: Path) -> tuple[str, int]:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as error:
        raise SourceMaterializerError("SOURCE_PATH_COLLISION", entry.path) from error
    command = [
        "git",
        "--no-pager",
        "-c",
        "protocol.allow=never",
        f"--git-dir={git_dir}",
        "cat-file",
        "blob",
        entry.object_id,
    ]
    try:
        process = subprocess.Popen(
            command,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=descriptor,
            stderr=subprocess.PIPE,
        )
        _unused, stderr = process.communicate()
        if process.returncode != 0:
            detail = stderr[:2_000].decode("utf-8", errors="replace").strip()
            _fail("GIT_BLOB_READ_FAILED", f"{entry.path}: {detail}")
        os.fsync(descriptor)
    except SourceMaterializerError:
        raise
    except OSError as error:
        raise SourceMaterializerError("GIT_BLOB_READ_FAILED", entry.path) from error
    finally:
        os.close(descriptor)
    digest, count, _metadata = _hash_regular_file(destination)
    os.chmod(destination, 0o555 if entry.mode == "100755" else 0o444)
    return digest, count


def _write_manifest(path: Path, value: dict[str, object]) -> bytes:
    encoded = _canonical_json(value) + b"\n"
    if len(encoded) > MAXIMUM_MANIFEST_BYTES:
        _fail("SOURCE_MANIFEST_TOO_LARGE")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o444)
    except OSError as error:
        raise SourceMaterializerError("SOURCE_MANIFEST_WRITE_FAILED", str(path)) from error
    return encoded


def _freeze_directories(root: Path) -> None:
    directories: list[Path] = [root]
    for current, names, _files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        for name in names:
            path = base / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                _fail("UNSAFE_SOURCE_ENTRY", str(path))
            directories.append(path)
    for directory in reversed(directories):
        _fsync_directory(directory)
        os.chmod(directory, 0o555)


def _remove_staging(path: Path) -> None:
    if not os.path.lexists(path):
        return
    for current, directories, files in os.walk(path, topdown=False, followlinks=False):
        base = Path(current)
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
        for name in files:
            try:
                (base / name).unlink()
            except FileNotFoundError:
                pass
        for name in directories:
            child = base / name
            try:
                if child.is_symlink():
                    child.unlink()
                else:
                    os.chmod(child, 0o700)
                    child.rmdir()
            except FileNotFoundError:
                pass
    try:
        path.rmdir()
    except FileNotFoundError:
        pass


def _strict_manifest(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > MAXIMUM_MANIFEST_BYTES:
        _fail("SOURCE_MANIFEST_INVALID", "size")

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _fail("SOURCE_MANIFEST_INVALID", f"duplicate key: {key}")
            result[key] = value
        return result

    def reject_float(_value: str) -> NoReturn:
        _fail("SOURCE_MANIFEST_INVALID", "floating-point value")

    def reject_constant(value: str) -> NoReturn:
        _fail("SOURCE_MANIFEST_INVALID", value)

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_float=reject_float,
            parse_constant=reject_constant,
        )
    except SourceMaterializerError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise SourceMaterializerError("SOURCE_MANIFEST_INVALID", "JSON") from error
    if type(value) is not dict or raw != _canonical_json(value) + b"\n":
        _fail("SOURCE_MANIFEST_INVALID", "noncanonical")
    return value


@dataclass(frozen=True, slots=True)
class MaterializedSource:
    name: str
    repository: str
    commit: str
    git_tree: str
    tree_sha256: str
    file_count: int
    total_bytes: int
    root: Path
    tree_path: Path
    manifest_path: Path
    manifest_sha256: str


class SourceMaterializer:
    """Own and audit exact source trees selected by a :class:`CoreLock`."""

    def __init__(
        self,
        data_root: str | os.PathLike[str],
        *,
        core_lock: CoreLock | None = None,
    ) -> None:
        supplied = Path(data_root).expanduser()
        if supplied.is_symlink():
            _fail("UNSAFE_SOURCE_CACHE_PATH", str(supplied))
        self.data_root = supplied.resolve(strict=False)
        self.core_lock = load_core_lock() if core_lock is None else core_lock

    def _source(self, name: str) -> LockedSource:
        if type(name) is not str or _SOURCE_NAME.fullmatch(name) is None:
            _fail("LOCKED_SOURCE_NAME_INVALID", str(name))
        try:
            source = self.core_lock.source(name)
        except ValueError as error:
            raise SourceMaterializerError("LOCKED_SOURCE_UNKNOWN", name) from error
        if (
            source.name != name
            or _SOURCE_NAME.fullmatch(source.name) is None
            or re.fullmatch(r"[0-9a-f]{40}", source.commit) is None
            or re.fullmatch(r"[0-9a-f]{64}", source.materialized_tree_sha256)
            is None
        ):
            _fail("LOCKED_SOURCE_IDENTITY_INVALID", name)
        return source

    def _paths(self, source: LockedSource) -> tuple[Path, Path, Path, Path]:
        source_root = self.data_root / "source-cache" / source.name
        root = source_root / source.commit
        return source_root, root, root / TREE_NAME, root / MANIFEST_NAME

    def _validate_partial_cache_path(self, source: LockedSource) -> None:
        """Reject an existing unsafe ancestor without creating missing ones."""

        for path in (
            self.data_root,
            self.data_root / "source-cache",
            self.data_root / "source-cache" / source.name,
        ):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                return
            except OSError as error:
                raise SourceMaterializerError(
                    "SOURCE_CACHE_CONFLICT", str(path)
                ) from error
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                _fail("SOURCE_CACHE_CONFLICT", f"unsafe ancestor: {path}")

    def ensure(
        self,
        name: str,
        *,
        local_repository: str | os.PathLike[str] | None = None,
        read_only: bool = False,
    ) -> MaterializedSource:
        """Return an audited cache entry, materializing it only when allowed.

        ``read_only=True`` performs no filesystem creation and never opens the
        local Git repository.  A missing entry fails explicitly.  In normal
        mode, ``local_repository`` is required only when the entry is absent.
        """

        source = self._source(name)
        _source_root, root, _tree, _manifest = self._paths(source)
        if os.path.lexists(root):
            return self._audit(source)
        if read_only:
            self._validate_partial_cache_path(source)
            _fail("SOURCE_NOT_MATERIALIZED", f"{source.name}@{source.commit}")
        if local_repository is None:
            _fail("LOCAL_GIT_REPOSITORY_REQUIRED", source.name)
        return self._materialize(source, Path(local_repository))

    def audit(self, name: str) -> MaterializedSource:
        """Audit one entry without creating directories or reading Git."""

        return self.ensure(name, read_only=True)

    def _prepare_cache_root(self, source: LockedSource) -> Path:
        try:
            if self.data_root.is_symlink():
                _fail("UNSAFE_SOURCE_CACHE_PATH", str(self.data_root))
            self.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            data_root = self.data_root.resolve(strict=True)
            cache = data_root / "source-cache"
            if cache.is_symlink():
                _fail("UNSAFE_SOURCE_CACHE_PATH", str(cache))
            cache.mkdir(mode=0o700, exist_ok=True)
            cache = cache.resolve(strict=True)
            if cache.parent != data_root or not cache.is_dir():
                _fail("UNSAFE_SOURCE_CACHE_PATH", str(cache))
            source_root = cache / source.name
            if source_root.is_symlink():
                _fail("UNSAFE_SOURCE_CACHE_PATH", str(source_root))
            source_root.mkdir(mode=0o700, exist_ok=True)
            source_root = source_root.resolve(strict=True)
            if source_root.parent != cache or not source_root.is_dir():
                _fail("UNSAFE_SOURCE_CACHE_PATH", str(source_root))
            return source_root
        except SourceMaterializerError:
            raise
        except OSError as error:
            raise SourceMaterializerError(
                "SOURCE_CACHE_UNAVAILABLE", str(self.data_root)
            ) from error

    def _existing_source_root(self, source: LockedSource) -> Path:
        """Validate cache ancestors without creating or resolving through links."""

        data_root = self.data_root
        cache = data_root / "source-cache"
        source_root = cache / source.name
        try:
            data_metadata = data_root.lstat()
            cache_metadata = cache.lstat()
            source_metadata = source_root.lstat()
        except OSError as error:
            raise SourceMaterializerError(
                "SOURCE_CACHE_CONFLICT", f"incomplete: {source_root}"
            ) from error
        for path, metadata in (
            (data_root, data_metadata),
            (cache, cache_metadata),
            (source_root, source_metadata),
        ):
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                _fail("SOURCE_CACHE_CONFLICT", f"unsafe ancestor: {path}")
        try:
            resolved_data = data_root.resolve(strict=True)
            resolved_cache = cache.resolve(strict=True)
            resolved_source = source_root.resolve(strict=True)
        except OSError as error:
            raise SourceMaterializerError("SOURCE_CACHE_CONFLICT", str(source_root)) from error
        if (
            resolved_data != data_root
            or resolved_cache.parent != resolved_data
            or resolved_source.parent != resolved_cache
        ):
            _fail("SOURCE_CACHE_CONFLICT", f"escaped cache root: {source_root}")
        return resolved_source

    def _materialize(self, source: LockedSource, repository: Path) -> MaterializedSource:
        git_dir = _resolve_git_dir(repository)
        source_root = self._prepare_cache_root(source)
        target = source_root / source.commit
        lock_path = source_root / ".materialize.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        with _PROCESS_LOCK:
            try:
                lock_descriptor = os.open(lock_path, flags, 0o600)
            except OSError as error:
                raise SourceMaterializerError(
                    "SOURCE_CACHE_LOCK_UNAVAILABLE", str(lock_path)
                ) from error
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
                if os.path.lexists(target):
                    return self._audit(source)
                git_tree, entries = _read_git_tree(git_dir, source)
                staging = Path(
                    tempfile.mkdtemp(prefix=f".{source.commit}.", dir=source_root)
                )
                try:
                    tree_path = staging / TREE_NAME
                    tree_path.mkdir(mode=0o700)
                    files: list[dict[str, object]] = []
                    total_bytes = 0
                    known_directories: set[str] = set()
                    for entry in entries:
                        relative = PurePosixPath(entry.path)
                        destination = tree_path.joinpath(*relative.parts)
                        _make_parents(tree_path, relative, known_directories)
                        digest, count = _write_blob(git_dir, entry, destination)
                        total_bytes += count
                        files.append(
                            {
                                "bytes": count,
                                "git_blob": entry.object_id,
                                "git_mode": entry.mode,
                                "path": entry.path,
                                "sha256": digest,
                            }
                        )
                    content = {"files": files, "git_tree": git_tree}
                    tree_sha256 = hashlib.sha256(_canonical_json(content)).hexdigest()
                    if tree_sha256 != source.materialized_tree_sha256:
                        _fail(
                            "LOCKED_TREE_DIGEST_MISMATCH",
                            f"{source.name}@{source.commit}",
                        )
                    manifest: dict[str, object] = {
                        "files": files,
                        "schema": MATERIALIZATION_SCHEMA,
                        "source": {
                            "commit": source.commit,
                            "git_tree": git_tree,
                            "name": source.name,
                            "repository": source.repository,
                        },
                        "summary": {
                            "file_count": len(files),
                            "total_bytes": total_bytes,
                            "tree_sha256": tree_sha256,
                        },
                    }
                    _write_manifest(staging / MANIFEST_NAME, manifest)
                    _freeze_directories(staging)
                    if os.path.lexists(target):
                        _fail("SOURCE_CACHE_CONFLICT", str(target))
                    try:
                        os.rename(staging, target)
                    except OSError as error:
                        raise SourceMaterializerError(
                            "SOURCE_CACHE_CONFLICT", str(target)
                        ) from error
                    _fsync_directory(source_root)
                finally:
                    _remove_staging(staging)
                return self._audit(source)
            finally:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                os.close(lock_descriptor)

    def _audit(self, source: LockedSource) -> MaterializedSource:
        source_root = self._existing_source_root(source)
        root = source_root / source.commit
        tree_path = root / TREE_NAME
        manifest_path = root / MANIFEST_NAME
        try:
            root_metadata = root.lstat()
            tree_metadata = tree_path.lstat()
            manifest_metadata = manifest_path.lstat()
        except OSError as error:
            raise SourceMaterializerError(
                "SOURCE_CACHE_CONFLICT", f"incomplete: {root}"
            ) from error
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_IMODE(root_metadata.st_mode) != 0o555
            or stat.S_ISLNK(tree_metadata.st_mode)
            or not stat.S_ISDIR(tree_metadata.st_mode)
            or stat.S_IMODE(tree_metadata.st_mode) != 0o555
            or stat.S_ISLNK(manifest_metadata.st_mode)
            or not stat.S_ISREG(manifest_metadata.st_mode)
            or stat.S_IMODE(manifest_metadata.st_mode) != 0o444
            or manifest_metadata.st_nlink != 1
        ):
            _fail("SOURCE_CACHE_CONFLICT", f"unsafe envelope: {root}")
        try:
            root_names = {entry.name for entry in os.scandir(root)}
        except OSError as error:
            raise SourceMaterializerError("SOURCE_CACHE_CONFLICT", str(root)) from error
        if root_names != {MANIFEST_NAME, TREE_NAME}:
            _fail("SOURCE_CACHE_CONFLICT", f"unexpected envelope entry: {root}")

        raw_manifest, manifest_digest, stable_manifest_metadata = (
            _read_bounded_stable_manifest(manifest_path)
        )
        if (
            stat.S_IMODE(stable_manifest_metadata.st_mode) != 0o444
            or stable_manifest_metadata.st_nlink != 1
        ):
            _fail("SOURCE_CACHE_CONFLICT", f"unsafe manifest: {manifest_path}")
        manifest = _strict_manifest(raw_manifest)
        if set(manifest) != {"files", "schema", "source", "summary"}:
            _fail("SOURCE_MANIFEST_INVALID", "envelope")
        raw_source = manifest.get("source")
        raw_summary = manifest.get("summary")
        raw_files = manifest.get("files")
        if (
            manifest.get("schema") != MATERIALIZATION_SCHEMA
            or type(raw_source) is not dict
            or set(raw_source) != {"commit", "git_tree", "name", "repository"}
            or raw_source.get("name") != source.name
            or raw_source.get("repository") != source.repository
            or raw_source.get("commit") != source.commit
            or type(raw_source.get("git_tree")) is not str
            or _OBJECT_ID.fullmatch(raw_source["git_tree"]) is None
            or type(raw_summary) is not dict
            or set(raw_summary) != {"file_count", "total_bytes", "tree_sha256"}
            or type(raw_files) is not list
        ):
            _fail("SOURCE_MANIFEST_INVALID", "identity")

        files: list[dict[str, object]] = []
        expected_paths: set[str] = set()
        expected_directories: set[str] = {"."}
        total_bytes = 0
        previous_key: bytes | None = None
        for row in raw_files:
            if type(row) is not dict or set(row) != {
                "bytes",
                "git_blob",
                "git_mode",
                "path",
                "sha256",
            }:
                _fail("SOURCE_MANIFEST_INVALID", "file row")
            path_value = row.get("path")
            try:
                path = _safe_git_path(path_value.encode("utf-8")) if type(path_value) is str else ""
            except UnicodeError as error:
                raise SourceMaterializerError("SOURCE_MANIFEST_INVALID", "path") from error
            key = path.encode("utf-8")
            if (
                not path
                or (previous_key is not None and key <= previous_key)
                or path in expected_paths
                or row.get("git_mode") not in ("100644", "100755")
                or type(row.get("git_blob")) is not str
                or _OBJECT_ID.fullmatch(row["git_blob"]) is None
                or type(row.get("sha256")) is not str
                or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None
                or type(row.get("bytes")) is not int
                or row["bytes"] < 0
            ):
                _fail("SOURCE_MANIFEST_INVALID", "file identity")
            previous_key = key
            expected_paths.add(path)
            relative = PurePosixPath(path)
            for parent in relative.parents:
                expected_directories.add(parent.as_posix())
            total_bytes += row["bytes"]
            files.append(row)

        content = {"files": files, "git_tree": raw_source["git_tree"]}
        tree_sha256 = hashlib.sha256(_canonical_json(content)).hexdigest()
        if (
            raw_summary.get("file_count") != len(files)
            or raw_summary.get("total_bytes") != total_bytes
            or raw_summary.get("tree_sha256") != tree_sha256
        ):
            _fail("SOURCE_MANIFEST_INVALID", "summary")
        if tree_sha256 != source.materialized_tree_sha256:
            _fail(
                "LOCKED_TREE_DIGEST_MISMATCH",
                f"{source.name}@{source.commit}",
            )

        observed_files: set[str] = set()
        observed_directories: set[str] = {"."}
        for current, directories, filenames in os.walk(
            tree_path, topdown=True, followlinks=False
        ):
            base = Path(current)
            relative_base = base.relative_to(tree_path)
            for name in directories:
                directory = base / name
                relative = (relative_base / name).as_posix()
                metadata = directory.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISDIR(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o555
                ):
                    _fail("SOURCE_CACHE_CONFLICT", f"unsafe directory: {relative}")
                observed_directories.add(relative)
            for name in filenames:
                path = base / name
                relative = (relative_base / name).as_posix()
                if relative not in expected_paths:
                    _fail("SOURCE_CACHE_CONFLICT", f"unexpected file: {relative}")
                observed_files.add(relative)

        if observed_files != expected_paths or observed_directories != expected_directories:
            _fail("SOURCE_CACHE_CONFLICT", "tree shape mismatch")
        by_path = {row["path"]: row for row in files}
        for relative in sorted(expected_paths, key=lambda value: value.encode("utf-8")):
            row = by_path[relative]
            path = tree_path.joinpath(*PurePosixPath(relative).parts)
            digest, count, metadata = _hash_regular_file(path)
            expected_mode = 0o555 if row["git_mode"] == "100755" else 0o444
            if (
                digest != row["sha256"]
                or count != row["bytes"]
                or stat.S_IMODE(metadata.st_mode) != expected_mode
                or metadata.st_nlink != 1
            ):
                _fail("SOURCE_CACHE_CONFLICT", f"file mismatch: {relative}")

        return MaterializedSource(
            name=source.name,
            repository=source.repository,
            commit=source.commit,
            git_tree=raw_source["git_tree"],
            tree_sha256=tree_sha256,
            file_count=len(files),
            total_bytes=total_bytes,
            root=root,
            tree_path=tree_path,
            manifest_path=manifest_path,
            manifest_sha256=manifest_digest,
        )
