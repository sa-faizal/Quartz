#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT
"""Collect and export CI evidence from Quartz, without a dashboard-side API token."""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ci_health.downstream import load_report
from ci_health.store import HealthStore


def _validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("Collection config must be an object")
    for key in ("repos", "release_sources"):
        items = config.get(key)
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            raise ValueError(f"{key} must be an array of nonempty strings")
    if not config["repos"] and not config["release_sources"]:
        raise ValueError("At least one repository or release source is required")
    for key, low, high in (
        ("days", 1, 90),
        ("history_days", 0, 90),
        ("max_runs", 1, 1000),
        ("max_attempts", 1, 100),
        ("max_correlated_runs", 0, 1000),
        ("max_requests", 1, 10000),
        ("max_artifacts", 1, 1000),
        ("stale_after_seconds", 0, 31_536_000),
        ("export_limit", 1, 10000),
    ):
        if key in config:
            value = config[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not low <= value <= high
            ):
                raise ValueError(f"{key} must be an integer between {low} and {high}")
    if "include_artifacts" in config and not isinstance(
        config["include_artifacts"], bool
    ):
        raise ValueError("include_artifacts must be a boolean")
    if "release_repo" in config and not isinstance(config["release_repo"], str):
        raise ValueError("release_repo must be owner/name")
    return config


def _guard_output(output: Path | None, *protected: Path | None) -> None:
    """An export must never replace the database or an input report/config."""
    if output is None:
        return
    for path in protected:
        if path is None:
            continue
        same = output.resolve() == path.resolve()
        if output.exists() and path.exists():
            same = same or output.samefile(path)
        if same:
            raise ValueError("Output must differ from the database and input files")


def write_json(path: Path, document: dict) -> None:
    """Replace the published feed atomically so clients never see partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            name = handle.name
            json.dump(
                document,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            handle.write("\n")
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("health-state/health.sqlite"))
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser(
        "collect", help="Collect release and workflow evidence, then export"
    )
    collect_parser.add_argument(
        "--config", type=Path, default=Path("config/ci-health.json")
    )
    collect_parser.add_argument(
        "--output", type=Path, default=Path("health-state/health.json")
    )
    collect_parser.add_argument("--days", type=int)
    collect_parser.add_argument("--max-runs", type=int)
    collect_parser.add_argument(
        "--include-artifacts", action="store_true", default=None
    )
    export_parser = sub.add_parser(
        "export", help="Export existing evidence without network access"
    )
    export_parser.add_argument("--output", type=Path, required=True)
    export_parser.add_argument("--stale-after-seconds", type=int, default=7200)
    export_parser.add_argument("--max-records", type=int, default=200)
    ingest = sub.add_parser(
        "ingest-downstream",
        help="Validate and import a maintainer-reviewed downstream report",
    )
    ingest.add_argument("--report", type=Path, required=True)
    ingest.add_argument("--output", type=Path)
    junit = sub.add_parser(
        "ingest-junit", help="Import an explicitly attributed local JUnit XML report"
    )
    junit.add_argument("--report", type=Path, required=True)
    junit.add_argument("--repo", required=True)
    junit.add_argument("--run-id", type=int, required=True)
    junit.add_argument("--run-attempt", type=int, required=True)
    junit.add_argument("--job-id", type=int)
    junit.add_argument("--arch")
    junit.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    store = None
    try:
        _guard_output(
            args.output,
            args.db,
            getattr(args, "report", None),
            getattr(args, "config", None),
        )
        args.db.parent.mkdir(parents=True, exist_ok=True)
        store = HealthStore(args.db)
        if args.command == "collect":
            from ci_health.collect import collect

            config = _validate_config(
                json.loads(args.config.read_text(encoding="utf-8"))
            )
            days = args.days if args.days is not None else config.get("days", 14)
            max_runs = (
                args.max_runs
                if args.max_runs is not None
                else config.get("max_runs", 50)
            )
            if not 1 <= days <= 90 or not 1 <= max_runs <= 1000:
                raise ValueError("days must be 1..90 and max-runs must be 1..1000")
            outcome = collect(
                store,
                repos=config["repos"],
                release_sources=config["release_sources"],
                release_repo=config.get("release_repo", "ROCm/rockrel"),
                days=days,
                max_runs=max_runs,
                max_attempts=config.get("max_attempts", 5),
                history_days=config.get("history_days", 0),
                max_correlated_runs=config.get("max_correlated_runs"),
                max_requests=config.get("max_requests", 500),
                max_artifacts=config.get("max_artifacts", 3),
                include_artifacts=(
                    args.include_artifacts
                    if args.include_artifacts is not None
                    else config.get("include_artifacts", False)
                ),
            )
            write_json(
                args.output,
                store.export(
                    stale_after_seconds=config.get("stale_after_seconds", 7200),
                    max_records=config.get("export_limit", 200),
                ),
            )
            print(json.dumps(outcome, indent=2))
            return 0 if outcome.get("status") in ("success", "complete") else 2
        if args.command == "ingest-downstream":
            report = load_report(args.report)
            release = store.release_document(report["release_id"])
            if release is None:
                raise ValueError(
                    "release_id is unknown: collect the release before importing its downstream evidence"
                )
            if report["build_variant"] != (release.get("build_variant") or "release"):
                raise ValueError(
                    "build_variant does not match the referenced Quartz release"
                )
            store.ingest_downstream(report)
            print("Downstream observation imported")
        if args.command == "ingest-junit":
            from ci_health.junit import parse_junit
            from ci_health.downstream import REPO_RE

            if (
                not REPO_RE.fullmatch(args.repo)
                or args.run_id < 1
                or args.run_attempt < 1
                or (args.job_id is not None and args.job_id < 1)
            ):
                raise ValueError("Expected owner/repo and positive run-id/run-attempt")
            if args.report.stat().st_size > 20_000_000:
                raise ValueError("JUnit report exceeds 20 MB limit")
            context = {
                "repo": args.repo,
                "run_id": args.run_id,
                "run_attempt": args.run_attempt,
                "source": args.report.name,
            }
            if args.job_id is not None:
                context["job_id"] = args.job_id
            if args.arch:
                context["arch"] = args.arch
            records = parse_junit(args.report.read_bytes(), context)
            store.ingest_tests(records)
            print(f"Imported {len(records)} JUnit cases")
        if args.output:
            write_json(
                args.output,
                store.export(
                    stale_after_seconds=getattr(args, "stale_after_seconds", 7200),
                    max_records=getattr(args, "max_records", 200),
                ),
            )
        return 0
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        print(f"CI health: {exc}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
