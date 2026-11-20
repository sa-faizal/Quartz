# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT
"""Validate downstream observations before storing them in Quartz.

Import is a trusted-maintainer operation. Public payloads cannot choose file
paths, run commands, or publish themselves. Report origins are retained so
manually imported evidence is distinguishable from collected GitHub results.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

STATUSES = {"success", "failure", "in_progress", "cancelled", "skipped"}
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_REPORT_BYTES = 2_000_000


def validate_report(report: dict) -> dict:
    """Return a constrained record, or reject incomplete/untraceable evidence."""
    if not isinstance(report, dict):
        raise ValueError("Downstream report must be an object")
    if str(report.get("schema_version", "")) != "1.0":
        raise ValueError("Downstream report requires schema_version 1.0")
    result = {"schema_version": "1.0", "origin": "maintainer_import"}
    for key in (
        "release_id",
        "project",
        "repo",
        "head_sha",
        "platform",
        "arch",
        "suite",
    ):
        value = report.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError(
                f"{key} must be a nonempty string of at most 512 characters"
            )
        result[key] = value
    if not REPO_RE.fullmatch(result["repo"]):
        raise ValueError("repo must be owner/name")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", result["head_sha"]):
        raise ValueError("head_sha must be a full 40-character Git commit")
    for key in ("run_id", "run_attempt"):
        value = report.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{key} must be a positive integer")
        result[key] = value
    status = report.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        raise ValueError("Invalid downstream status")
    result["status"] = status
    observed_at = report.get("observed_at")
    try:
        timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("timezone missing")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("observed_at must be an ISO timestamp with timezone") from exc
    if timestamp > datetime.now(timezone.utc):
        raise ValueError("observed_at cannot be in the future")
    result["observed_at"] = (
        timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    expected_url = (
        f"https://github.com/{result['repo']}/actions/runs/{result['run_id']}"
    )
    url = report.get("url", expected_url)
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or url.rstrip("/") != expected_url
    ):
        raise ValueError("url must point to the report's GitHub workflow run")
    result["url"] = url
    variant = report.get("build_variant", "release")
    if not isinstance(variant, str) or not variant or len(variant) > 80:
        raise ValueError("Invalid build_variant")
    result["build_variant"] = variant
    return result


def load_report(path: Path) -> dict:
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("Downstream report exceeds size limit")
    return validate_report(json.loads(path.read_text(encoding="utf-8")))
