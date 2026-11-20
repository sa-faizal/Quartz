# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ci_health.downstream import validate_report


def report():
    return {
        "schema_version": "1.0",
        "release_id": "nightly:10.3.0a20261119:release",
        "project": "debug-tools",
        "repo": "ROCm/TheRock",
        "head_sha": "a" * 40,
        "run_id": 123,
        "run_attempt": 2,
        "platform": "linux",
        "arch": "gfx942",
        "suite": "debug-tools-smoke",
        "status": "failure",
        "observed_at": "2026-01-01T00:00:00Z",
    }


def test_requires_traceable_workflow_and_commit():
    record = validate_report(report())
    assert record["origin"] == "maintainer_import"
    assert record["url"] == "https://github.com/ROCm/TheRock/actions/runs/123"
    with pytest.raises(ValueError, match="url must"):
        validate_report({**report(), "url": "https://example.com/other-run"})
    with pytest.raises(ValueError, match="head_sha"):
        validate_report({**report(), "head_sha": "main"})


@pytest.mark.parametrize(
    "change",
    [
        {"run_attempt": True},
        {"run_id": -1},
        {"status": "green"},
        {"observed_at": "2026-01-01"},
        {"schema_version": "99"},
        {"repo": "../../bad"},
        {"observed_at": "9999-01-01T00:00:00Z"},
    ],
)
def test_rejects_invalid_report(change):
    with pytest.raises(ValueError):
        validate_report({**report(), **change})
