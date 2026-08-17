from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pmw_platform import cli


def _result(tmp_path: Path) -> object:
    return SimpleNamespace(
        name="agent-math-frontier",
        repository="https://github.com/example/agent-math-frontier.git",
        commit="a" * 40,
        git_tree="b" * 40,
        tree_sha256="c" * 64,
        manifest_sha256="d" * 64,
        file_count=12,
        total_bytes=345,
        tree_path=tmp_path / "source-cache" / "tree",
        manifest_path=tmp_path / "source-cache" / "manifest.json",
    )


@pytest.mark.parametrize("operation", ["materialize", "audit"])
def test_source_commands_route_exact_data_root_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    emitted: list[object] = []
    observed: dict[str, object] = {}
    result = _result(tmp_path)

    class FakeMaterializer:
        def __init__(self, data_root: Path) -> None:
            observed["data_root"] = data_root

        def ensure(self, name: str, *, local_repository: Path) -> object:
            observed["ensure"] = (name, local_repository)
            return result

        def audit(self, name: str) -> object:
            observed["audit"] = name
            return result

    monkeypatch.setattr(cli, "SourceMaterializer", FakeMaterializer)
    monkeypatch.setattr(cli, "_emit", emitted.append)
    arguments = [
        "--data-root",
        str(tmp_path),
        "source",
        operation,
        "agent-math-frontier",
    ]
    if operation == "materialize":
        arguments.extend(["--local-repo", str(tmp_path / "local.git")])

    assert cli.main(arguments) == 0
    assert observed["data_root"] == tmp_path
    if operation == "materialize":
        assert observed["ensure"] == (
            "agent-math-frontier",
            tmp_path / "local.git",
        )
    else:
        assert observed["audit"] == "agent-math-frontier"
    assert emitted == [
        {
            "schema": "PMW_LOCKED_SOURCE_READY_1",
            "operation": (
                "MATERIALIZE_OR_AUDIT"
                if operation == "materialize"
                else "READ_ONLY_AUDIT"
            ),
            "name": result.name,
            "repository": result.repository,
            "commit": result.commit,
            "git_tree": result.git_tree,
            "tree_sha256": result.tree_sha256,
            "manifest_sha256": result.manifest_sha256,
            "file_count": 12,
            "total_bytes": 345,
            "tree_path": str(result.tree_path),
            "manifest_path": str(result.manifest_path),
            "network_calls": 0,
            "network_calls_authority": (
                "HOST_ASSERTION_GIT_PROTOCOL_ALLOW_NEVER"
            ),
        }
    ]
