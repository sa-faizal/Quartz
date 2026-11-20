# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Offline command-line integration and publication safety."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ci_health import cli
from ci_health.store import HealthStore

CLI_PATH = Path(__file__).resolve().parents[1] / "cli.py"


def invoke(db, *args):
    return subprocess.run(
        [sys.executable, str(CLI_PATH), "--db", str(db), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def seed_release(db):
    with HealthStore(db) as store:
        return store.ingest_release(
            {
                "schema_version": "2.2",
                "rocm_version": "10.3.0a20261119",
                "build_date": "20261119",
                "release_type": "nightly",
                "build_variant": "release",
                "summary": {"overall_status": "failure"},
            },
            "fixture://release",
        )


def downstream(release_id):
    return {
        "schema_version": "1.0",
        "release_id": release_id,
        "project": "debug-tools",
        "repo": "ROCm/TheRock",
        "head_sha": "a" * 40,
        "platform": "linux",
        "arch": "gfx942",
        "suite": "smoke",
        "run_id": 42,
        "run_attempt": 1,
        "status": "failure",
        "observed_at": "2026-01-01T00:00:00Z",
        "build_variant": "release",
    }


def test_cli_imports_downstream_and_junit_then_exports_offline(tmp_path):
    db = tmp_path / "health.sqlite"
    release_id = seed_release(db)
    report = tmp_path / "downstream.json"
    report.write_text(json.dumps(downstream(release_id)), encoding="utf-8")
    imported = invoke(db, "ingest-downstream", "--report", report)
    assert imported.returncode == 0, imported.stderr
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite name="hip"><testcase name="add"/><testcase name="sub"><failure message="expected 1"/></testcase></testsuite>',
        encoding="utf-8",
    )
    imported = invoke(
        db,
        "ingest-junit",
        "--report",
        junit,
        "--repo",
        "ROCm/TheRock",
        "--run-id",
        42,
        "--run-attempt",
        1,
    )
    assert imported.returncode == 0, imported.stderr
    output = tmp_path / "site" / "health.json"
    exported = invoke(db, "export", "--output", output)
    assert exported.returncode == 0, exported.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["summary"]["downstream_runs"] == 1
    assert data["summary"]["test_cases"] == 2
    assert data["summary"]["failed_tests"] == 1
    assert data["downstream"][0]["release_id"] == release_id
    assert data["downstream"][0]["origin"] == "maintainer_import"


@pytest.mark.parametrize(
    "change",
    [
        {"status": []},
        {"run_id": True},
        {"build_variant": "asan-debug"},
        {"release_id": "missing"},
    ],
)
def test_invalid_downstream_never_mutates_existing_evidence(tmp_path, change):
    db = tmp_path / "health.sqlite"
    release_id = seed_release(db)
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({**downstream(release_id), **change}), encoding="utf-8"
    )
    result = invoke(db, "ingest-downstream", "--report", report)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    with HealthStore(db) as store:
        assert store.export()["downstream"] == []


def test_export_cannot_overwrite_database_or_input_report(tmp_path):
    db = tmp_path / "health.sqlite"
    release_id = seed_release(db)
    before = db.read_bytes()
    result = invoke(db, "export", "--output", db)
    assert result.returncode == 1
    assert db.read_bytes() == before
    report = tmp_path / "report.json"
    text = json.dumps(downstream(release_id))
    report.write_text(text, encoding="utf-8")
    result = invoke(db, "ingest-downstream", "--report", report, "--output", report)
    assert result.returncode == 1
    assert report.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    "config",
    [
        [],
        {},
        {"repos": "ROCm/TheRock", "release_sources": []},
        {"repos": ["ROCm/TheRock"], "release_sources": [], "days": "14"},
    ],
)
def test_malformed_config_exits_cleanly_without_network(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    result = invoke(
        tmp_path / "state.sqlite",
        "collect",
        "--config",
        path,
        "--output",
        tmp_path / "feed.json",
    )
    assert result.returncode == 1
    assert "CI health:" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "feed.json").exists()


def test_failed_json_serialization_preserves_previous_published_feed(tmp_path):
    output = tmp_path / "health.json"
    output.write_text('{"previous":true}', encoding="utf-8")
    with pytest.raises(ValueError):
        cli.write_json(output, {"invalid": float("nan")})
    assert json.loads(output.read_text(encoding="utf-8")) == {"previous": True}
    assert [path.name for path in tmp_path.iterdir()] == ["health.json"]


def test_failed_atomic_replace_preserves_previous_feed_and_cleans_temporary_file(
    tmp_path, monkeypatch
):
    output = tmp_path / "health.json"
    output.write_text('{"previous":true}', encoding="utf-8")

    def fail_replace(*_):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(cli.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        cli.write_json(output, {"new": True})
    assert json.loads(output.read_text(encoding="utf-8")) == {"previous": True}
    assert [path.name for path in tmp_path.iterdir()] == ["health.json"]


def test_corrupt_database_returns_actionable_error(tmp_path):
    db = tmp_path / "state.sqlite"
    db.write_bytes(b"not sqlite")
    result = invoke(db, "export", "--output", tmp_path / "health.json")
    assert result.returncode == 1
    assert "CI health:" in result.stderr
    assert "Traceback" not in result.stderr
