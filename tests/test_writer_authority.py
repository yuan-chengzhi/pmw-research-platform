"""Security boundary tests for the host-only PMW writer authority file."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path

import pytest

from pmw_platform.world import (
    PmwWriterAuthority,
    ResearchWorldError,
    load_writer_authority,
)


def _authority_value() -> dict[str, object]:
    return {
        "schema": "PMW_WRITER_AUTHORITY_1",
        "channel_ref": "channel:test",
        "invocation_ref": "invocation:test",
        "process_ref": "process:test",
        "principal_ref": "principal:test",
        "episode_ref": "episode:test",
        "capability_ref": "capability:test",
        "scope_ref": "scope:test",
        "policy_ref": "policy:test",
        "policy_fingerprint": "a" * 64,
        "maximum_calls": 8,
        "maximum_delivery_attempts": 16,
        "maximum_content_bytes": 65_536,
        "maximum_parent_refs": 16,
    }


def _write_authority(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_load_writer_authority_accepts_owner_only_exact_schema(tmp_path: Path) -> None:
    path = tmp_path / "writer-authority.json"
    expected = _authority_value()
    _write_authority(path, expected)

    authority = load_writer_authority(path)

    assert isinstance(authority, PmwWriterAuthority)
    assert authority.to_value() == expected


@pytest.mark.parametrize("mode", [0o604, 0o620, 0o660, 0o666])
def test_load_writer_authority_rejects_group_or_other_access(
    tmp_path: Path, mode: int
) -> None:
    path = tmp_path / "writer-authority.json"
    _write_authority(path, _authority_value())
    path.chmod(mode)

    with pytest.raises(ResearchWorldError) as caught:
        load_writer_authority(path)

    assert caught.value.code == "WRITER_AUTHORITY_UNSAFE"


def test_load_writer_authority_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "authority-target.json"
    _write_authority(target, _authority_value())
    alias = tmp_path / "authority-alias.json"
    alias.symlink_to(target)

    with pytest.raises(ResearchWorldError) as caught:
        load_writer_authority(alias)

    assert caught.value.code == "WRITER_AUTHORITY_UNSAFE"


def test_load_writer_authority_rejects_file_not_owned_by_effective_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "writer-authority.json"
    _write_authority(path, _authority_value())
    observed_uid = path.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: observed_uid + 1)

    with pytest.raises(ResearchWorldError) as caught:
        load_writer_authority(path)

    assert caught.value.code == "WRITER_AUTHORITY_UNSAFE"


def test_load_writer_authority_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "writer-authority.json"
    raw = json.dumps(_authority_value(), separators=(",", ":"))
    path.write_text(
        raw[:-1] + ',"maximum_calls":9}\n',
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(ResearchWorldError) as caught:
        load_writer_authority(path)

    assert caught.value.code == "MALFORMED_WRITER_AUTHORITY"
    assert caught.value.detail == "JSON"


@pytest.mark.parametrize(
    ("mutate", "detail"),
    [
        (lambda value: value.update({"unexpected": True}), "fields"),
        (lambda value: value.pop("scope_ref"), "fields"),
        (lambda value: value.update(schema="PMW_WRITER_AUTHORITY_0"), "schema"),
        (lambda value: value.update(channel_ref="not a valid ref"), "channel_ref"),
        (lambda value: value.update(policy_fingerprint="f" * 63), "policy_fingerprint"),
        (lambda value: value.update(maximum_calls=True), "maximum_calls"),
        (
            lambda value: value.update(
                maximum_calls=17, maximum_delivery_attempts=16
            ),
            "limits",
        ),
        (lambda value: value.update(maximum_content_bytes=65_537), "limits"),
        (lambda value: value.update(maximum_parent_refs=17), "limits"),
    ],
)
def test_load_writer_authority_rejects_malformed_fields(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], object],
    detail: str,
) -> None:
    value = _authority_value()
    mutate(value)
    path = tmp_path / "writer-authority.json"
    _write_authority(path, value)

    with pytest.raises(ResearchWorldError) as caught:
        load_writer_authority(path)

    assert caught.value.code == "MALFORMED_WRITER_AUTHORITY"
    assert caught.value.detail == detail


def test_load_writer_authority_rejects_floating_point_json(tmp_path: Path) -> None:
    value = _authority_value()
    value["maximum_calls"] = 1.0
    path = tmp_path / "writer-authority.json"
    _write_authority(path, value)

    with pytest.raises(ResearchWorldError) as caught:
        load_writer_authority(path)

    assert caught.value.code == "MALFORMED_WRITER_AUTHORITY"
    assert caught.value.detail == "JSON"
