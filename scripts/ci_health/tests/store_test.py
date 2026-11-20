# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""History, retry and missing-data correctness for the CI health store."""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store import HealthStore


def release(date="20261118", status="success"):
    return {
        "schema_version": "2.2",
        "rocm_version": "10.3.0a" + date,
        "release_type": "nightly",
        "build_date": date,
        "build_variant": "release",
        "summary": {
            "overall_status": status,
            "linux": {
                "status": status,
                "architectures": ["gfx942"],
                "rocm": {"build": {"status": "success"}, "test": {status: 1}},
            },
        },
        "pipelines": {"rocm": {"test": {"linux": {"gfx942": {"status": status}}}}},
    }


def run(attempt, conclusion):
    return {
        "repo": "ROCm/TheRock",
        "id": 100,
        "run_attempt": attempt,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": "2026-11-19T00:00:00Z",
        "observed_at": "2026-11-19T01:00:00Z",
    }


@pytest.mark.parametrize("conclusion", ["startup_failure", "action_required"])
def test_attention_outcomes_are_counted_consistently_in_summary_and_recovery(
    tmp_path, conclusion
):
    with HealthStore(tmp_path / "health.sqlite") as store:
        store.ingest_run(run(1, conclusion))
        exported = store.export()
        assert exported["summary"]["failed_runs"] == 1
        assert exported["trends"]["daily_runs"][0]["failed_runs"] == 1
        store.ingest_run(run(2, "success"))
        assert store.export()["summary"]["recovered_runs"] == 1


def test_changed_release_is_preserved_and_same_snapshot_is_idempotent(tmp_path):
    with HealthStore(tmp_path / "health.sqlite") as store:
        document = release(status="in_progress")
        identity = store.ingest_release(
            document, "https://source/latest.json", "2026-11-19T00:00:00Z"
        )
        changed = release(status="failure")
        assert (
            store.ingest_release(
                changed, "https://source/latest.json", "2026-11-19T01:00:00Z"
            )
            == identity
        )
        store.ingest_release(
            changed, "https://source/latest.json", "2026-11-19T02:00:00Z"
        )
        exported = store.export(now="2026-11-19T02:01:00Z")
        assert exported["summary"]["releases"] == 1
        assert exported["releases"][0]["snapshot_count"] == 2
        assert exported["releases"][0]["overall_status"] == "failure"
        assert exported["releases"][0]["observed_at"] == "2026-11-19T02:00:00Z"
    with HealthStore(tmp_path / "health.sqlite") as reopened:
        assert reopened.export()["releases"][0]["snapshot_count"] == 2


def test_latest_success_after_observed_failure_is_recovery_not_extra_run():
    with HealthStore(":memory:") as store:
        store.ingest_run(run(1, "failure"))
        store.ingest_run(run(2, "success"))
        exported = store.export(now="2026-11-19T02:00:00Z")
        assert exported["summary"]["runs"] == 1
        assert exported["summary"]["failed_runs"] == 0
        assert exported["summary"]["recovered_runs"] == 1
        assert len(exported["run_attempts"]) == 2
        assert exported["runs"][0]["first_attempt_observed"]


def test_unobserved_previous_attempt_does_not_imply_recovery():
    with HealthStore(":memory:") as store:
        store.ingest_run(run(3, "success"))
        exported = store.export()
        assert exported["summary"]["recovered_runs"] == 0
        assert not exported["runs"][0]["first_attempt_observed"]
        assert any("first attempts" in gap for gap in exported["gaps"])


def test_no_data_and_failed_collector_are_not_fresh_healthy_data():
    with HealthStore(":memory:") as store:
        store.record_collection(
            {
                "status": "failure",
                "observed_at": "2026-11-19T02:00:00Z",
                "errors": ["HTTP 403"],
            }
        )
        empty = store.export(now="2026-11-19T02:00:01Z")
        assert empty["freshness"]["status"] == "missing"
        assert empty["collection"]["status"] == "failure"
        assert len(empty["gaps"]) >= 4
        store.ingest_release(release(), "source", "2026-11-18T00:00:00Z")
        stale = store.export(now="2026-11-19T02:00:01Z")
        assert stale["freshness"]["status"] == "stale"


def test_tests_are_idempotent_and_only_latest_attempt_counts():
    case = {
        "repo": "ROCm/TheRock",
        "run_id": 100,
        "run_attempt": 1,
        "suite": "hip",
        "classname": "Suite",
        "name": "test_add",
        "status": "fail",
        "source": "report.xml",
    }
    with HealthStore(":memory:") as store:
        store.ingest_tests([case, case])
        passed = {**case, "run_attempt": 2, "status": "pass"}
        store.ingest_tests([passed])
        exported = store.export()
        assert exported["summary"]["test_cases"] == 1
        assert exported["summary"]["failed_tests"] == 0
        assert len(exported["test_attempts"]) == 2
        # A newer run without a report must not inherit a previous test pass.
        store.ingest_run(run(3, "success"))
        assert store.export()["tests"] == []


def test_bad_test_batch_is_atomic():
    with HealthStore(":memory:") as store:
        with pytest.raises(ValueError, match="Missing required"):
            store.ingest_tests(
                [
                    {"repo": "R/P", "run_id": 1, "name": "ok", "status": "pass"},
                    {"repo": "R/P", "run_id": 1, "status": "fail"},
                ]
            )
        assert store.export()["tests"] == []


def test_regressions_require_comparable_dimension_and_prior_success():
    with HealthStore(":memory:") as store:
        store.ingest_release(release(), "source", "2026-11-18T12:00:00Z")
        current = release("20261119", "failure")
        current["pipelines"]["rocm"]["test"]["linux"]["gfx950"] = {"status": "failure"}
        store.ingest_release(current, "source", "2026-11-19T12:00:00Z")
        newest = store.export()["releases"][0]
        assert len(newest["regressions"]) == 1
        assert newest["regressions"][0]["dimension"] == "rocm / test / linux / gfx942"
        variant = copy.deepcopy(current)
        variant["build_variant"] = "asan-debug"
        store.ingest_release(variant, "source")
        variant_export = next(
            r for r in store.export()["releases"] if r["build_variant"] == "asan-debug"
        )
        assert variant_export["regressions"] == []


def test_missing_job_creation_does_not_invent_queue_duration():
    with HealthStore(":memory:") as store:
        record = run(1, "success")
        record["jobs"] = [
            {
                "id": 1,
                "started_at": "2026-11-19T00:00:00Z",
                "completed_at": "2026-11-19T00:01:00Z",
            }
        ]
        store.ingest_run(record)
        job = store.export()["runs"][0]["jobs"][0]
        assert job["queue_seconds"] is None
        assert job["duration_seconds"] == 60


def test_downstream_retries_remain_evidence_but_do_not_duplicate_counts():
    with HealthStore(":memory:") as store:
        record = {
            "release_id": "release-1",
            "project": "debug-tools",
            "repo": "R/P",
            "run_id": 1,
            "run_attempt": 1,
            "status": "failure",
        }
        store.ingest_downstream(record)
        store.ingest_downstream({**record, "run_attempt": 2, "status": "success"})
        exported = store.export()
        assert exported["summary"]["downstream_runs"] == 1
        assert len(exported["downstream_attempts"]) == 2
        assert exported["downstream"][0]["status"] == "success"


@pytest.mark.parametrize("status", ["failure", "error", "failed"])
def test_failed_poll_does_not_leave_fresh_status_after_recent_data(status):
    with HealthStore(":memory:") as store:
        store.ingest_release(release(), "source", "2026-11-19T02:00:00Z")
        store.record_collection(
            {
                "status": status,
                "observed_at": "2026-11-19T02:01:00Z",
                "errors": ["rate limited"],
            }
        )
        exported = store.export(now="2026-11-19T02:01:01Z")
        assert exported["freshness"]["status"] == "stale"
        assert "failed" in exported["freshness"]["reason"]
        assert not exported["freshness"]["collection_complete"]


def test_partial_collection_reports_gaps_independently_from_observation_age():
    with HealthStore(":memory:") as store:
        store.ingest_release(release(), "source", "2026-11-19T02:00:00Z")
        store.record_collection(
            {
                "status": "partial",
                "observed_at": "2026-11-19T02:01:00Z",
                "warnings": [{"stage": "limit"}],
            }
        )
        current = store.export(now="2026-11-19T02:02:00Z")
        assert current["freshness"]["status"] == "fresh"
        assert not current["freshness"]["collection_complete"]
        assert current["freshness"]["reason"] == "Latest collection has coverage gaps."
        assert current["collection"]["status"] == "partial"
        assert any("coverage gaps" in gap for gap in current["gaps"])
        assert (
            store.export(now="2026-11-20T02:02:00Z")["freshness"]["status"] == "stale"
        )


def test_export_limits_do_not_destroy_history_or_change_summary():
    with HealthStore(":memory:") as store:
        store.ingest_run(run(1, "failure"))
        store.ingest_run(run(2, "success"))
        limited = store.export(max_records=1)
        assert len(limited["run_attempts"]) == 1
        assert limited["export_limits"]["truncated_lists"]["run_attempts"] == 2
        assert limited["summary"]["recovered_runs"] == 1
        assert len(store.export()["run_attempts"]) == 2


def test_failed_job_refresh_preserves_previous_details_as_historical_only():
    with HealthStore(":memory:") as store:
        record = {
            **run(1, "failure"),
            "jobs_complete": True,
            "jobs": [{"id": 7, "name": "Test", "conclusion": "failure"}],
        }
        store.ingest_run(record)
        for hour in (2, 3):
            store.ingest_run(
                {
                    **record,
                    "jobs_complete": False,
                    "jobs": [],
                    "observed_at": f"2026-11-19T0{hour}:00:00Z",
                }
            )
            current = store.export()["runs"][0]
            assert current["jobs"] == []
            assert current["last_known_jobs"][0]["id"] == 7
            assert current["last_known_jobs_observed_at"] == "2026-11-19T01:00:00Z"
        store.ingest_run({**record, "jobs": [{"id": 8, "name": "Current"}]})
        current = store.export()["runs"][0]
        assert current["jobs"][0]["id"] == 8
        assert "last_known_jobs" not in current


def test_exported_attempt_history_is_metadata_only_without_losing_stored_jobs():
    with HealthStore(":memory:") as store:
        for attempt in (1, 2):
            store.ingest_run(
                {
                    **run(attempt, "failure" if attempt == 1 else "success"),
                    "html_url": "https://github.com/ROCm/TheRock/actions/runs/100",
                    "jobs": [{"id": attempt, "steps": [{"name": "large detail"}]}],
                    "last_known_jobs": [{"id": 99}],
                    "steps": [{"name": "legacy"}],
                }
            )
        exported = store.export()
        assert len(exported["run_attempts"]) == 2
        for attempt in exported["run_attempts"]:
            assert {
                "repo",
                "id",
                "run_attempt",
                "conclusion",
                "created_at",
                "html_url",
            } <= attempt.keys()
            assert "jobs" not in attempt
            assert "last_known_jobs" not in attempt
            assert "steps" not in attempt
        assert exported["runs"][0]["jobs"][0]["id"] == 2
        retained = store.connection.execute(
            "SELECT document FROM runs WHERE attempt = 1"
        ).fetchone()[0]
        assert '"large detail"' in retained
        assert exported["summary"]["recovered_runs"] == 1
        assert exported["coverage"]["run_attempts_observed"] == 2


def test_bounded_current_tests_show_failures_and_errors_before_passes():
    with HealthStore(":memory:") as store:
        cases = []
        for index, status in enumerate(("error", "fail", "pass", "skipped")):
            cases.append(
                {
                    "repo": "ROCm/TheRock",
                    "run_id": 1,
                    "run_attempt": 1,
                    "suite": "hip",
                    "name": f"case{index}",
                    "status": status,
                    "observed_at": f"2026-11-19T0{index}:00:00Z",
                }
            )
        store.ingest_tests(cases)
        bounded = store.export(max_records=2)
        assert {case["status"] for case in bounded["tests"]} == {"error", "fail"}
        assert bounded["summary"]["test_cases"] == 4
        assert bounded["summary"]["failed_tests"] == 2
        assert bounded["export_limits"]["truncated_lists"]["tests"] == 4
        assert len(store.export()["tests"]) == 4
