#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT
"""Submit a validated downstream observation through an authorized workflow dispatch."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ci_health.downstream import REPO_RE, load_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--quartz-repo", default="sa-faizal/Quartz")
    parser.add_argument("--ref", default="develop")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Dispatch the report; default only validates",
    )
    args = parser.parse_args(argv)
    try:
        report = load_report(args.report)
        if not REPO_RE.fullmatch(args.quartz_repo):
            raise ValueError("quartz-repo must be owner/name")
        if not args.send:
            print(json.dumps(report, indent=2))
            print("Validated only; use --send to dispatch", file=sys.stderr)
            return 0
        payload = {"ref": args.ref, "inputs": {"report_json": json.dumps(report)}}
        # gh reads the existing credential or GH_TOKEN; neither is printed or
        # embedded in process arguments. Cross-repo writes require explicit access.
        result = subprocess.run(
            [
                "gh",
                "api",
                "--hostname",
                "github.com",
                "--method",
                "POST",
                f"repos/{args.quartz_repo}/actions/workflows/ingest_ci_health.yml/dispatches",
                "--input",
                "-",
            ],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            print(
                "Dispatch failed; confirm workflow availability and repository write permission",
                file=sys.stderr,
            )
            return 1
        print(
            "Downstream report dispatched. Check the import workflow for validation and publication status."
        )
        return 0
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Downstream report: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
