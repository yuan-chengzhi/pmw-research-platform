from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pmw_platform.runtime.safety import (
    BoundedCaptureAccumulator,
    CaptureLimits,
    Disposition,
    REQUIRED_SAFETY_CODES,
    SafetyProfileError,
    load_named_profile,
    validate_profile,
)


PROFILES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "pmw_platform"
    / "profiles"
)


def test_profiles_load_and_classify_the_complete_code_surface() -> None:
    research = load_named_profile("research-default", profiles_dir=PROFILES)
    strict = load_named_profile("strict-experiment", profiles_dir=PROFILES)

    assert set(research.dispositions) == REQUIRED_SAFETY_CODES
    assert set(strict.dispositions) == REQUIRED_SAFETY_CODES
    assert research.disposition("PROCESS_GROUP_CLEANUP_FAILED") is Disposition.SESSION_STOP
    assert research.disposition("TOOL_TIMEOUT") is Disposition.JOB_STOP
    assert research.disposition("CONTEXT_HARD_LIMIT") is Disposition.JOB_STOP
    assert research.disposition("PMW_AUTHORITY_VIOLATION") is Disposition.REJECT
    assert research.disposition("ARTIFACT_TOO_LARGE") is Disposition.REJECT
    assert research.disposition("WORKSPACE_HARDLINK_OBSERVED") is Disposition.WARN
    assert research.disposition("WORKSPACE_UNSAFE_SYMLINK") is Disposition.WARN
    assert strict.disposition("WORKSPACE_UNSAFE_SYMLINK") is Disposition.SESSION_STOP
    assert strict.disposition("RUNTIME_CACHE_TOTAL_BYTES_EXCEEDED") is Disposition.SESSION_STOP
    assert research.disposition("RUNTIME_CACHE_TOTAL_BYTES_EXCEEDED") is Disposition.JOB_STOP


def test_research_default_has_no_independent_75_mb_file_rule() -> None:
    research = load_named_profile("research-default", profiles_dir=PROFILES)
    strict = load_named_profile("strict-experiment", profiles_dir=PROFILES)
    m03_batch_bytes = 75_318_870

    assert research.workspace.maximum_file_bytes is None
    assert research.workspace_file_limit_disposition(m03_batch_bytes) is None
    assert (
        strict.workspace_file_limit_disposition(m03_batch_bytes)
        is Disposition.SESSION_STOP
    )


def test_retained_cap_truncates_without_terminating_capture() -> None:
    limits = CaptureLimits(
        maximum_retained_bytes=2 * 1024,
        maximum_observed_bytes=16 * 1024,
        tail_bytes=128,
    )
    capture = BoundedCaptureAccumulator(limits)
    payload = bytes(range(256)) * 32  # 8 KiB

    first = capture.append(payload[:1024])
    second = capture.append(payload[1024:])
    snapshot = capture.finalize()

    assert first.disposition is None
    assert second.disposition is None
    assert not second.safety_cap_crossed
    assert snapshot.observed_bytes == 8 * 1024
    assert snapshot.retained_bytes == 2 * 1024
    assert snapshot.retained == payload[: 2 * 1024]
    assert snapshot.tail == payload[-128:]
    assert snapshot.observed_sha256 == hashlib.sha256(payload).hexdigest()
    assert snapshot.truncated
    assert not snapshot.observed_safety_cap_exceeded
    assert snapshot.terminal_disposition is None


def test_observed_cap_signals_only_job_terminal_and_keeps_accounting() -> None:
    limits = CaptureLimits(
        maximum_retained_bytes=2 * 1024,
        maximum_observed_bytes=6 * 1024,
        tail_bytes=64,
    )
    capture = BoundedCaptureAccumulator(limits)
    first_payload = b"a" * (6 * 1024)
    final_payload = b"b" * 1024

    before = capture.append(first_payload)
    crossing = capture.append(final_payload)
    snapshot = capture.finalize()

    assert before.disposition is None
    assert crossing.safety_cap_crossed
    assert crossing.disposition is Disposition.JOB_STOP
    assert snapshot.terminal_disposition is Disposition.JOB_STOP
    assert snapshot.observed_bytes == 7 * 1024
    assert snapshot.retained_bytes == 2 * 1024
    assert snapshot.observed_sha256 == hashlib.sha256(
        first_payload + final_payload
    ).hexdigest()


def test_profile_validation_rejects_missing_or_unsafe_policy() -> None:
    raw = json.loads((PROFILES / "research-default.json").read_text())
    del raw["dispositions"]["OUTPUT_TRUNCATED"]
    with pytest.raises(SafetyProfileError, match="dispositions keys"):
        validate_profile(raw)

    raw = json.loads((PROFILES / "research-default.json").read_text())
    raw["dispositions"]["OBSERVED_OUTPUT_SAFETY_CAP"] = "WARN"
    with pytest.raises(SafetyProfileError, match="must be JOB_STOP"):
        validate_profile(raw)


def test_capture_rejects_non_job_observed_cap_disposition() -> None:
    limits = CaptureLimits(1024, 2048, 128)
    with pytest.raises(ValueError, match="must be JOB_STOP"):
        BoundedCaptureAccumulator(
            limits,
            observed_cap_disposition=Disposition.SESSION_STOP,
        )


def test_disk_guard_uses_larger_absolute_or_fractional_reserve() -> None:
    profile = load_named_profile("research-default", profiles_dir=PROFILES)
    assert profile.disk_guard.required_free_bytes(100 * 1024**3) == 20 * 1024**3
    assert profile.disk_guard.required_free_bytes(500 * 1024**3) == 50 * 1024**3


def test_capture_is_immutable_after_finalize() -> None:
    capture = BoundedCaptureAccumulator(CaptureLimits(1024, 2048, 64))
    capture.append(b"done")
    capture.finalize()
    with pytest.raises(RuntimeError, match="finalized"):
        capture.append(b"late")
