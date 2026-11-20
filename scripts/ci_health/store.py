# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Durable observations and a read-only JSON view of CI health.

Current status is an observation, not immutable history. Raw changed release/run
documents and test outcomes are retained separately from their latest values.
All attempt-aware counts use the newest *observed* attempt. A successful rerun
after an observed failure is recovery evidence, not proof of a flaky test.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FAILURE_CONCLUSIONS = {"failure", "timed_out", "startup_failure", "action_required"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _seconds(start: str | None, end: str | None) -> float | None:
    first, last = _time(start), _time(end)
    return max(0, (last - first).total_seconds()) if first and last else None


def _required(record: dict, fields: tuple[str, ...]) -> None:
    absent = [key for key in fields if record.get(key) in (None, "")]
    if absent:
        raise ValueError("Missing required fields: " + ", ".join(absent))


def _matrix(node: Any, path: tuple = ()) -> dict[str, str]:
    """Comparable leaf identities include platform, arch and framework versions."""
    result: dict[str, str] = {}
    if isinstance(node, dict):
        if "status" in node and "variants" not in node:
            result[" / ".join(path)] = node["status"]
        for key, value in node.items():
            if isinstance(value, dict):
                result.update(_matrix(value, (*path, key)))
            elif key == "variants" and isinstance(value, list):
                for variant in value:
                    if not isinstance(variant, dict):
                        continue
                    identity = {
                        k: v
                        for k, v in variant.items()
                        if k
                        not in {
                            "status",
                            "run_id",
                            "run_attempt",
                            "started_at",
                            "completed_at",
                            "created_at",
                            "updated_at",
                        }
                        and not isinstance(v, (list, dict))
                    }
                    result.update(_matrix(variant, (*path, _json(identity))))
    return result


class HealthStore:
    """SQLite-backed CI observations; no external database service is required."""

    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS release_snapshots (
              release_id TEXT NOT NULL, digest TEXT NOT NULL, source TEXT NOT NULL,
              observed_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
              document TEXT NOT NULL, PRIMARY KEY(release_id, digest));
            CREATE TABLE IF NOT EXISTS runs (
              repo TEXT NOT NULL, run_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
              observed_at TEXT NOT NULL, document TEXT NOT NULL,
              PRIMARY KEY(repo, run_id, attempt));
            CREATE TABLE IF NOT EXISTS run_snapshots (
              repo TEXT NOT NULL, run_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
              digest TEXT NOT NULL, observed_at TEXT NOT NULL, document TEXT NOT NULL,
              PRIMARY KEY(repo, run_id, attempt, digest));
            CREATE TABLE IF NOT EXISTS test_cases (
              identity TEXT PRIMARY KEY, repo TEXT NOT NULL, run_id INTEGER NOT NULL,
              attempt INTEGER NOT NULL, observed_at TEXT NOT NULL, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS test_snapshots (
              identity TEXT NOT NULL, digest TEXT NOT NULL, observed_at TEXT NOT NULL,
              document TEXT NOT NULL, PRIMARY KEY(identity, digest));
            CREATE TABLE IF NOT EXISTS downstream (
              identity TEXT PRIMARY KEY, observed_at TEXT NOT NULL, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS downstream_snapshots (
              identity TEXT NOT NULL, digest TEXT NOT NULL, observed_at TEXT NOT NULL,
              document TEXT NOT NULL, PRIMARY KEY(identity, digest));
            CREATE TABLE IF NOT EXISTS collections (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL,
              document TEXT NOT NULL);
            """
        )

    def __enter__(self) -> HealthStore:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def release_document(self, release_id: str) -> dict | None:
        """Look up a release without depending on the bounded dashboard export."""
        row = self.connection.execute(
            """SELECT document FROM release_snapshots WHERE release_id = ?
            ORDER BY last_seen_at DESC, rowid DESC LIMIT 1""",
            (release_id,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def ingest_release(
        self, document: dict, source: str, observed_at: str | None = None
    ) -> str:
        _required(document, ("rocm_version", "build_date"))
        identity = {
            k: document.get(k, "")
            for k in ("release_type", "rocm_version", "build_date", "build_variant")
        }
        release_id = _hash(identity)[:24]
        observed_at = observed_at or _now()
        with self.connection:
            self.connection.execute(
                """INSERT INTO release_snapshots VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(release_id,digest) DO UPDATE SET
                last_seen_at=excluded.last_seen_at, source=excluded.source""",
                (
                    release_id,
                    _hash(document),
                    source,
                    observed_at,
                    observed_at,
                    _json(document),
                ),
            )
        return release_id

    def ingest_run(self, record: dict) -> None:
        _required(record, ("repo", "id"))
        record = dict(record)
        record["id"] = int(record["id"])
        record["run_attempt"] = int(record.get("run_attempt") or 1)
        if record["run_attempt"] < 1:
            raise ValueError("run_attempt must be positive")
        observed_at = record.setdefault("observed_at", _now())
        values = (record["repo"], int(record["id"]), record["run_attempt"])
        if record.get("jobs_complete") is False:
            prior_row = self.connection.execute(
                "SELECT document FROM runs WHERE repo = ? AND run_id = ? AND attempt = ?",
                values,
            ).fetchone()
            if prior_row:
                prior = json.loads(prior_row[0])
                prior_jobs = (
                    prior.get("last_known_jobs")
                    if prior.get("jobs_complete") is False
                    else prior.get("jobs")
                )
                if prior_jobs:
                    # Keep old detail out of the current `jobs` list. Consumers
                    # may inspect it explicitly but cannot count it as fresh.
                    record["last_known_jobs"] = prior_jobs
                    record["last_known_jobs_observed_at"] = prior.get(
                        "last_known_jobs_observed_at"
                    ) or prior.get("observed_at")
        encoded = _json(record)
        # Observation time is collection metadata, not a changed workflow result.
        digest = _hash({k: v for k, v in record.items() if k != "observed_at"})
        with self.connection:
            self.connection.execute(
                """INSERT INTO runs VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(repo,run_id,attempt) DO UPDATE SET
                observed_at=excluded.observed_at, document=excluded.document""",
                (*values, observed_at, encoded),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO run_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                (*values, digest, observed_at, encoded),
            )

    def ingest_tests(self, records: list[dict]) -> None:
        # Validate before the transaction so a malformed batch cannot partly apply.
        for record in records:
            _required(record, ("repo", "run_id", "name", "status"))
            if record["status"] not in {"pass", "fail", "error", "skipped"}:
                raise ValueError("Unknown test status: " + str(record["status"]))
        with self.connection:
            for incoming in records:
                record = dict(incoming)
                record["run_id"] = int(record["run_id"])
                attempt = int(record.get("run_attempt") or 1)
                if attempt < 1:
                    raise ValueError("run_attempt must be positive")
                record["run_attempt"] = attempt
                observed = record.setdefault("observed_at", _now())
                identity = _hash(
                    {
                        key: record.get(key, "")
                        for key in (
                            "repo",
                            "run_id",
                            "run_attempt",
                            "job_id",
                            "suite",
                            "name",
                            "classname",
                            "source",
                            "arch",
                        )
                    }
                )
                digest = _hash({k: v for k, v in record.items() if k != "observed_at"})
                encoded = _json(record)
                self.connection.execute(
                    """INSERT INTO test_cases VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(identity) DO UPDATE SET
                    observed_at=excluded.observed_at, document=excluded.document""",
                    (
                        identity,
                        record["repo"],
                        record["run_id"],
                        attempt,
                        observed,
                        encoded,
                    ),
                )
                self.connection.execute(
                    "INSERT OR IGNORE INTO test_snapshots VALUES (?, ?, ?, ?)",
                    (identity, digest, observed, encoded),
                )

    def ingest_downstream(self, record: dict) -> None:
        _required(record, ("release_id", "project", "repo", "run_id", "status"))
        record = dict(record)
        record["run_attempt"] = int(record.get("run_attempt") or 1)
        observed = record.setdefault("observed_at", _now())
        identity = _hash(
            {
                key: record.get(key, "")
                for key in (
                    "release_id",
                    "project",
                    "repo",
                    "run_id",
                    "run_attempt",
                    "platform",
                    "arch",
                    "suite",
                    "build_variant",
                )
            }
        )
        digest = _hash({k: v for k, v in record.items() if k != "observed_at"})
        with self.connection:
            self.connection.execute(
                """INSERT INTO downstream VALUES (?, ?, ?)
                ON CONFLICT(identity) DO UPDATE SET
                observed_at=excluded.observed_at, document=excluded.document""",
                (identity, observed, _json(record)),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO downstream_snapshots VALUES (?, ?, ?, ?)",
                (identity, digest, observed, _json(record)),
            )

    def record_collection(self, record: dict) -> None:
        record = dict(record)
        observed = record.setdefault("observed_at", _now())
        with self.connection:
            self.connection.execute(
                "INSERT INTO collections(observed_at, document) VALUES (?, ?)",
                (observed, _json(record)),
            )

    def _documents(self, table: str) -> list[dict]:
        # Table names are internal literals, never input or a SQL parameter.
        queries = {
            "runs": "SELECT document FROM runs ORDER BY observed_at DESC",
            "test_cases": "SELECT document FROM test_cases ORDER BY observed_at DESC",
            "downstream": "SELECT document FROM downstream ORDER BY observed_at DESC",
            "collections": "SELECT document FROM collections ORDER BY sequence DESC",
        }
        return [json.loads(row[0]) for row in self.connection.execute(queries[table])]

    def export(
        self,
        now: str | None = None,
        stale_after_seconds: int = 7200,
        max_records: int = 5000,
    ) -> dict:
        if stale_after_seconds < 0:
            raise ValueError("stale_after_seconds must be nonnegative")
        if max_records < 1:
            raise ValueError("max_records must be positive")
        generated_at = now or _now()
        if not _time(generated_at):
            raise ValueError("now must be an ISO 8601 timestamp")
        releases = self._releases()
        attempts = self._documents("runs")
        latest: dict[tuple, dict] = {}
        for run in attempts:
            key = (run["repo"], run["id"])
            if key not in latest or run["run_attempt"] > latest[key]["run_attempt"]:
                latest[key] = dict(run)
        runs = sorted(
            latest.values(), key=lambda r: r.get("created_at") or "", reverse=True
        )
        for run in runs:
            earlier = [
                r
                for r in attempts
                if (r["repo"], r["id"]) == (run["repo"], run["id"])
                and r["run_attempt"] < run["run_attempt"]
            ]
            run["recovered"] = run.get("conclusion") == "success" and any(
                r.get("conclusion") in FAILURE_CONCLUSIONS for r in earlier
            )
            run["observed_attempts"] = len(earlier) + 1
            run["first_attempt_observed"] = run["run_attempt"] == 1 or any(
                r["run_attempt"] == 1 for r in earlier
            )
            for job in run.get("jobs", []):
                job.setdefault(
                    "queue_seconds",
                    _seconds(job.get("created_at"), job.get("started_at")),
                )
                job.setdefault(
                    "duration_seconds",
                    _seconds(job.get("started_at"), job.get("completed_at")),
                )
        all_tests = self._documents("test_cases")
        latest_test_attempt: dict[tuple, int] = {}
        for test in all_tests:
            key = (test["repo"], test["run_id"])
            latest_test_attempt[key] = max(
                latest_test_attempt.get(key, 1), test["run_attempt"]
            )
        tests = [
            t
            for t in all_tests
            if t["run_attempt"]
            == latest.get(
                (t["repo"], t["run_id"]),
                {"run_attempt": latest_test_attempt[(t["repo"], t["run_id"])]},
            )["run_attempt"]
        ]
        # The bounded browser feed must keep actionable failures visible even
        # when a large successful report was imported more recently.
        tests.sort(key=lambda test: test["status"] not in {"fail", "error"})
        downstream_attempts = self._documents("downstream")
        downstream_latest: dict[tuple, dict] = {}
        for item in downstream_attempts:
            key = tuple(
                item.get(k, "")
                for k in (
                    "release_id",
                    "project",
                    "repo",
                    "run_id",
                    "platform",
                    "arch",
                    "suite",
                    "build_variant",
                )
            )
            if (
                key not in downstream_latest
                or item["run_attempt"] > downstream_latest[key]["run_attempt"]
            ):
                downstream_latest[key] = item
        collections = self._documents("collections")
        collection = (
            collections[0] if collections else {"status": "missing", "errors": []}
        )
        observations = [r["observed_at"] for r in releases]
        observations += [r["observed_at"] for r in attempts]
        observations += [r["observed_at"] for r in all_tests]
        observations += [r["observed_at"] for r in downstream_attempts]
        latest_observed = (
            max(
                observations,
                key=lambda x: _time(x) or datetime.min.replace(tzinfo=timezone.utc),
            )
            if observations
            else None
        )
        age = _seconds(latest_observed, generated_at)
        freshness = {
            "status": (
                "missing"
                if age is None
                else ("stale" if age > stale_after_seconds else "fresh")
            ),
            "latest_observed_at": latest_observed,
            "age_seconds": age,
            "stale_after_seconds": stale_after_seconds,
        }
        if (
            collections
            and collection.get("status") in {"error", "failure", "failed"}
            and age is not None
        ):
            freshness["status"] = "stale"
            freshness["reason"] = (
                "Latest collection failed; retained data may be out of date."
            )
        freshness["collection_complete"] = collection.get("status") in {
            "success",
            "ok",
            "complete",
        }
        if collection.get("status") == "partial":
            # A bounded but recent collection has coverage gaps, not old data.
            # Keep age and completeness independent for dashboard consumers.
            freshness["reason"] = "Latest collection has coverage gaps."
        tested_runs = {(t["repo"], t["run_id"], t["run_attempt"]) for t in tests}
        completed_runs = [r for r in runs if r.get("status") == "completed"]
        gaps = []
        if not releases:
            gaps.append("No Quartz release status has been collected.")
        if not runs:
            gaps.append("No GitHub workflow/job details have been collected.")
        if any(run.get("jobs_complete") is False for run in runs):
            gaps.append(
                "Some current job lists are unavailable; last_known_jobs, when present, are retained historical evidence."
            )
        if len(tested_runs) < len(completed_runs) or not tests:
            gaps.append(
                "Individual test results cover only imported reports; missing reports are not passing tests."
            )
        if not downstream_latest:
            gaps.append("No downstream compatibility reports have been submitted.")
        if any(r["run_attempt"] > 1 and not r["first_attempt_observed"] for r in runs):
            gaps.append(
                "Some first attempts are unavailable; recovery counts are lower bounds."
            )
        if collection.get("status") == "partial":
            gaps.append(
                "The latest collection has coverage gaps; inspect collection errors and limits for uncollected sources."
            )
        elif collection.get("status") not in {"success", "ok", "complete"}:
            gaps.append(
                "The latest collection is missing or failed; retained data may be out of date."
            )
        gaps.append(
            "Root cause, ownership, coverage percentages and performance require explicit source evidence."
        )
        # Attempt history is a lightweight index. Full job/step observations
        # remain durable in SQLite and current jobs stay on `runs`; repeating
        # those trees here can multiply the browser payload by every rerun.
        attempt_metadata = [
            {
                key: value
                for key, value in attempt.items()
                if key
                in {
                    "repo",
                    "id",
                    "run_attempt",
                    "name",
                    "event",
                    "head_sha",
                    "branch",
                    "status",
                    "conclusion",
                    "created_at",
                    "started_at",
                    "updated_at",
                    "completed_at",
                    "observed_at",
                    "html_url",
                    "release_id",
                    "jobs_complete",
                    "last_known_jobs_observed_at",
                }
            }
            for attempt in attempts
        ]
        exported_lists = {
            "releases": releases,
            "runs": runs,
            "run_attempts": attempt_metadata,
            "tests": tests,
            "test_attempts": all_tests,
            "downstream": list(downstream_latest.values()),
            "downstream_attempts": downstream_attempts,
        }
        truncated = {
            key: len(items)
            for key, items in exported_lists.items()
            if len(items) > max_records
        }
        if truncated:
            gaps.append(
                "Some detail lists are limited for export size; all observations remain in the database."
            )
        daily: dict[str, dict] = {}
        for run in runs:
            day = (run.get("created_at") or run.get("observed_at") or "")[:10]
            if not day:
                continue
            bucket = daily.setdefault(
                day, {"date": day, "runs": 0, "failed_runs": 0, "recovered_runs": 0}
            )
            bucket["runs"] += 1
            bucket["failed_runs"] += int(run.get("conclusion") in FAILURE_CONCLUSIONS)
            bucket["recovered_runs"] += int(run["recovered"])
        last_green: dict[tuple, dict] = {}
        for release in releases:
            key = (release.get("release_type"), release.get("build_variant"))
            if key not in last_green and release["overall_status"] == "success":
                last_green[key] = {
                    "release_type": key[0],
                    "build_variant": key[1],
                    "release_id": release["id"],
                    "rocm_version": release["rocm_version"],
                    "build_date": release["build_date"],
                }
        return {
            "schema_version": "1.0",
            "generated_at": generated_at,
            "freshness": freshness,
            "collection": collection,
            "summary": {
                "releases": len(releases),
                "runs": len(runs),
                "failed_runs": sum(
                    r.get("conclusion") in FAILURE_CONCLUSIONS for r in runs
                ),
                "running_runs": sum(
                    r.get("status")
                    in {"queued", "in_progress", "waiting", "pending", "requested"}
                    for r in runs
                ),
                "recovered_runs": sum(r["recovered"] for r in runs),
                "test_cases": len(tests),
                "failed_tests": sum(t["status"] in {"fail", "error"} for t in tests),
                "downstream_runs": len(downstream_latest),
            },
            "coverage": {
                "run_attempts_observed": len(attempts),
                "completed_runs": len(completed_runs),
                "runs_with_test_reports": len(tested_runs),
                "test_result_unit": "individual test case",
                "quartz_counter_unit": "matrix entry",
                "first_attempts_observed": sum(
                    r["first_attempt_observed"] for r in runs
                ),
            },
            **{key: items[:max_records] for key, items in exported_lists.items()},
            "export_limits": {
                "max_records_per_list": max_records,
                "truncated_lists": truncated,
            },
            "trends": {"daily_runs": [daily[key] for key in sorted(daily)]},
            "last_green": list(last_green.values()),
            "gaps": gaps,
        }

    def _releases(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT rowid, * FROM release_snapshots ORDER BY last_seen_at DESC, rowid DESC"
        ).fetchall()
        latest: dict[str, sqlite3.Row] = {}
        snapshots: dict[str, int] = {}
        for row in rows:
            latest.setdefault(row["release_id"], row)
            snapshots[row["release_id"]] = snapshots.get(row["release_id"], 0) + 1
        releases = []
        for release_id, row in latest.items():
            raw = json.loads(row["document"])
            summary = raw.get("summary", {})
            platforms = []
            for name in ("linux", "windows"):
                data = summary.get(name)
                if not isinstance(data, dict):
                    continue
                pipelines = {
                    k: v for k, v in data.items() if isinstance(v, dict) and k != "urls"
                }
                platforms.append(
                    {
                        "name": name,
                        "status": data.get("status", "unknown"),
                        "build_status": data.get("rocm", {})
                        .get("build", {})
                        .get("status", "unknown"),
                        "test_counts": data.get("rocm", {}).get("test", {}),
                        "architectures": data.get("architectures", []),
                        "urls": data.get("urls", {}),
                        "pipelines": pipelines,
                    }
                )
            releases.append(
                {
                    "id": release_id,
                    **{
                        k: raw.get(k)
                        for k in (
                            "rocm_version",
                            "release_type",
                            "build_date",
                            "build_variant",
                            "therock_commit",
                            "trigger_workflow_run_id",
                            "trigger_run_attempt",
                            "created_at",
                            "completed_at",
                        )
                    },
                    "source": row["source"],
                    "observed_at": row["last_seen_at"],
                    "source_updated_at": raw.get("status_json_last_updated"),
                    "overall_status": summary.get("overall_status", "unknown"),
                    "platforms": platforms,
                    "pipelines": raw.get("pipelines", {}),
                    "snapshot_count": snapshots[release_id],
                    "regressions": [],
                    "matrix": _matrix(raw.get("pipelines", {})),
                }
            )
        releases.sort(
            key=lambda r: (
                r.get("build_date") or "",
                r.get("created_at") or "",
                r["id"],
            ),
            reverse=True,
        )
        for index, current in enumerate(releases):
            previous = next(
                (
                    r
                    for r in releases[index + 1 :]
                    if (r.get("release_type"), r.get("build_variant"))
                    == (current.get("release_type"), current.get("build_variant"))
                    and r.get("build_date") != current.get("build_date")
                ),
                None,
            )
            if previous:
                current["compared_to_release_id"] = previous["id"]
                current["regressions"] = [
                    {
                        "dimension": dimension,
                        "previous": "success",
                        "current": "failure",
                    }
                    for dimension, status in current["matrix"].items()
                    if status == "failure"
                    and previous["matrix"].get(dimension) == "success"
                ]
        return releases
