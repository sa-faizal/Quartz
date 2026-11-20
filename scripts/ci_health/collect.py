# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Collect release snapshots, Actions attempts/jobs and optional JUnit evidence.

The dashboard reads only Quartz's export. This collector is the boundary to
upstream sources. Every limit and failed source is reported as partial coverage.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from .junit import parse_junit_archive
except ImportError:
    from junit import parse_junit_archive

API = "https://api.github.com"
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
JUNIT_ARTIFACT_PATTERN = re.compile(
    r"quartz-junit-attempt-([1-9][0-9]*)(?:-[A-Za-z0-9][A-Za-z0-9_.-]{0,127})?"
)


class CollectionError(RuntimeError):
    """A bounded source read could not complete."""


class _StripAuthRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise CollectionError("Refusing non-HTTPS artifact redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
        return redirected


class GitHubClient:
    """GitHub-only authenticated reads; redirected downloads lose credentials."""

    def __init__(self, token=None, timeout=30):
        self.token = token or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
        self.timeout = timeout
        self.opener = build_opener(_StripAuthRedirect())
        if not self.token and shutil.which("gh"):
            # Capture in process memory only. Never print/store the token or pass
            # it as a subprocess argument.
            try:
                result = subprocess.run(
                    ["gh", "auth", "token", "--hostname", "github.com"],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                if result.returncode == 0:
                    self.token = result.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                # Public repositories remain readable without authentication,
                # subject to GitHub's lower unauthenticated rate limit.
                pass

    def _read(self, path, max_bytes, accept):
        url = path if path.startswith("https://") else API + path
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise CollectionError("Only api.github.com source requests are allowed")
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Quartz-CI-Health",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with self.opener.open(
                Request(url, headers=headers), timeout=self.timeout
            ) as response:
                body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise CollectionError("GitHub response exceeds size limit")
            return body
        except HTTPError as exc:
            # Do not include response bodies, signed URLs, request headers or
            # credential-bearing query strings in the exported collection errors.
            raise CollectionError(f"GitHub HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CollectionError("GitHub request failed") from exc

    def get(self, path):
        try:
            document = json.loads(
                self._read(path, 20_000_000, "application/vnd.github+json")
            )
        except (ValueError, UnicodeError) as exc:
            raise CollectionError("GitHub returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise CollectionError("GitHub returned an unexpected document")
        return document

    def download_artifact(self, url, max_bytes):
        return self._read(url, max_bytes, "application/vnd.github+json")


def _timestamp(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _duration(start, end):
    if not start or not end:
        return None
    try:
        seconds = (_timestamp(end) - _timestamp(start)).total_seconds()
        return seconds if seconds >= 0 else None
    except (TypeError, ValueError):
        return None


def _normalize_job(job):
    return {
        key: job.get(key)
        for key in (
            "id",
            "name",
            "status",
            "conclusion",
            "created_at",
            "started_at",
            "completed_at",
            "html_url",
            "runner_name",
            "labels",
            "steps",
        )
    } | {
        "queue_seconds": _duration(job.get("created_at"), job.get("started_at")),
        "duration_seconds": _duration(job.get("started_at"), job.get("completed_at")),
    }


def _normalize_run(repo, run, jobs, release_id=None):
    completed = None
    if run.get("status") == "completed":
        ends = [job["completed_at"] for job in jobs if job.get("completed_at")]
        completed = max(ends) if ends else run.get("updated_at")
    return {
        "repo": repo,
        "id": run["id"],
        "run_attempt": int(run.get("run_attempt") or 1),
        "name": run.get("name", ""),
        "event": run.get("event", ""),
        "head_sha": run.get("head_sha", ""),
        "branch": run.get("head_branch", ""),
        "status": run.get("status", "unknown"),
        "conclusion": run.get("conclusion"),
        "created_at": run.get("created_at"),
        "started_at": run.get("run_started_at"),
        "updated_at": run.get("updated_at"),
        "completed_at": completed,
        "html_url": run.get("html_url", ""),
        "jobs": jobs,
        "release_id": release_id,
        "pull_requests": [
            {"number": pr.get("number"), "url": pr.get("html_url")}
            for pr in run.get("pull_requests", [])
        ],
    }


def _release_loader(source):
    consumer_dir = str(Path(__file__).resolve().parents[1] / "consumer")
    if consumer_dir not in sys.path:
        sys.path.insert(0, consumer_dir)
    from read_status_json import load_status

    return load_status(source)


def _run_ids(document):
    ids = set()

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("run_id"), int):
                ids.add(value["run_id"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document.get("pipelines", {}))
    trigger = document.get("trigger_workflow_run_id")
    if isinstance(trigger, int):
        ids.add(trigger)
    return ([trigger] if isinstance(trigger, int) else []) + sorted(ids - {trigger})


def _history_sources(source, document, history_days):
    """Derive bounded nightly siblings from a strict public raw-GitHub URL.

    The release document cannot supply a destination URL or path. Sources with
    ambiguous refs, URL credentials/queries, or an unknown filename fail closed.
    History is evidence-only: importing it must not fan out Actions collection.
    """
    if document.get("release_type") != "nightly":
        return []
    build_date = document.get("build_date", "")
    if not isinstance(build_date, str) or not re.fullmatch(r"[0-9]{8}", build_date):
        raise ValueError("Nightly history requires a YYYYMMDD build date")
    try:
        anchor = datetime.strptime(build_date, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("Nightly history requires a valid build date") from exc
    parsed = urlsplit(source)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "raw.githubusercontent.com"
        or parsed.query
        or parsed.fragment
        or len(parts) not in (5, 6)
        or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts[:3])
        or any(part in {".", ".."} for part in parts)
        or parts[3] != "nightly"
    ):
        raise ValueError("Nightly history requires an unambiguous raw GitHub source")
    if len(parts) == 5:
        match = re.fullmatch(
            r"latest(?:_good)?(?P<suffix>_[A-Za-z0-9-]+)?\.json", parts[-1]
        )
    else:
        if not re.fullmatch(r"[0-9]{8}", parts[-2]):
            raise ValueError("Nightly history source has an invalid date path")
        match = re.fullmatch(r"status(?P<suffix>_[A-Za-z0-9-]+)?\.json", parts[-1])
    if match is None:
        raise ValueError("Nightly history source has an unsupported status filename")
    suffix = match.group("suffix") or ""
    base = "https://raw.githubusercontent.com/" + "/".join(parts[:4])
    dated = [
        f'{base}/{(anchor - timedelta(days=day)).strftime("%Y%m%d")}/status{suffix}.json'
        for day in range(1, history_days + 1)
    ]
    return dated + [f"{base}/latest_good{suffix}.json"]


def collect(
    store,
    *,
    repos,
    release_sources,
    days=14,
    max_runs=100,
    token=None,
    now=None,
    include_artifacts=False,
    max_artifact_bytes=20_000_000,
    client=None,
    release_loader=None,
    max_attempts=5,
    release_repo="ROCm/rockrel",
    max_correlated_runs=None,
    max_requests=500,
    max_artifacts=3,
    history_days=0,
):
    """Collect all event types in a bounded creation-date window.

    ``max_runs`` is per repository. Referenced release runs are fetched outside
    that cap, preserving release correlation. ``max_attempts`` bounds rerun
    history per run; omitted older attempts are explicitly reported. Optional
    JUnit is accepted only when artifact metadata proves the exact run attempt.
    A partial/error return is durable coverage evidence, never a green result.
    ``history_days`` additionally imports prior nightly documents and the
    last-good pointer without expanding workflow/job collection.
    """
    if (
        days < 1
        or max_runs < 1
        or max_attempts < 1
        or max_artifact_bytes < 1
        or max_requests < 1
        or max_artifacts < 1
    ):
        raise ValueError("Collection bounds must be positive")
    if not isinstance(history_days, int) or not 0 <= history_days <= 90:
        raise ValueError("history_days must be between 0 and 90")
    max_correlated_runs = (
        max_runs if max_correlated_runs is None else max_correlated_runs
    )
    if max_correlated_runs < 0:
        raise ValueError("Correlated run bound must not be negative")
    for repo in [*repos, release_repo]:
        if not REPO_PATTERN.fullmatch(repo):
            raise ValueError("Repository must have owner/name form")
    started = _timestamp(now)
    observed_at = _iso(started)
    since = _iso(started - timedelta(days=days))
    client = client or GitHubClient(token)
    release_loader = release_loader or _release_loader
    result = {
        "observed_at": observed_at,
        "started_at": observed_at,
        "status": "success",
        "errors": [],
        "warnings": [],
        "repos": list(repos),
        "release_repo": release_repo,
        "window_days": days,
        "window_basis": "workflow run creation time",
        "max_runs_per_repo": max_runs,
        "max_attempts_per_run": max_attempts,
        "artifact_collection_enabled": include_artifacts,
        "release_documents": 0,
        "history_documents": 0,
        "history_days": history_days,
        "runs": 0,
        "attempts": 0,
        "jobs": 0,
        "test_cases": 0,
        "artifact_reports": 0,
        "unattributed_artifacts": 0,
        "repository_coverage": [],
        "max_requests": max_requests,
        "requests": 0,
        "max_correlated_runs": max_correlated_runs,
        "max_artifacts_per_run": max_artifacts,
        "job_fallbacks": [],
    }

    def problem(stage, identity, exc=None, *, warning=False):
        entry = {"stage": stage, "source": identity}
        if exc is not None:
            # Only controlled errors are useful here. Arbitrary exception text
            # may include signed URLs or credentials from an injected client.
            entry["error"] = (
                str(exc)
                if isinstance(exc, (CollectionError, ValueError))
                else type(exc).__name__
            )
        result["warnings" if warning else "errors"].append(entry)
        result["status"] = "partial"

    def get(path):
        if result["requests"] >= max_requests:
            raise CollectionError("Collection request budget exhausted")
        result["requests"] += 1
        return client.get(path)

    release_runs = {}
    history_sources = []
    seen_release_sources = set(release_sources)
    for source in release_sources:
        try:
            loaded = release_loader(source)
            document = loaded if isinstance(loaded, dict) else loaded.raw
            release_id = store.ingest_release(document, source, observed_at)
            result["release_documents"] += 1
            for run_id in _run_ids(document):
                release_runs[(release_repo, run_id)] = release_id
            if history_days:
                try:
                    history_sources.extend(
                        _history_sources(source, document, history_days)
                    )
                except ValueError as exc:
                    problem("history_unavailable", source, exc, warning=True)
        except Exception as exc:
            problem("release", source, exc)

    for source in history_sources:
        if source in seen_release_sources:
            continue
        seen_release_sources.add(source)
        try:
            loaded = release_loader(source)
            document = loaded if isinstance(loaded, dict) else loaded.raw
            if document.get("release_type") != "nightly":
                raise ValueError("Historical nightly source returned another stream")
            store.ingest_release(document, source, observed_at)
            result["release_documents"] += 1
            result["history_documents"] += 1
        except Exception as exc:
            problem("history", source, exc, warning=True)

    page_completeness = {}

    def pages(path, field, *, cap=None, page_size=100):
        page_completeness[path] = True
        values = []
        page = 1
        while page <= 100:
            separator = "&" if "?" in path else "?"
            response = get(f"{path}{separator}per_page={page_size}&page={page}")
            batch = response.get(field)
            if not isinstance(batch, list):
                raise CollectionError(f"Missing {field} array")
            if cap is not None and len(values) + len(batch) >= cap:
                remaining = cap - len(values)
                values.extend(batch[:remaining])
                total = response.get("total_count")
                if len(batch) > remaining or (
                    isinstance(total, int) and total > len(values)
                ):
                    page_completeness[path] = False
                    problem("limit", path, warning=True)
                return values
            values.extend(batch)
            total = response.get("total_count")
            if type(total) is int and len(values) >= total:
                return values
            if len(batch) < page_size:
                return values
            page += 1
        page_completeness[path] = False
        problem("pagination_limit", path, warning=True)
        return values

    selected = {}
    for repo in repos:
        try:
            query = urlencode({"created": f">={since}"})
            runs_path = f"/repos/{repo}/actions/runs?{query}"
            runs = pages(
                runs_path,
                "workflow_runs",
                cap=min(max_runs, 1000),
            )
            for run in runs:
                selected[(repo, int(run["id"]))] = run
            result["repository_coverage"].append(
                {
                    "repo": repo,
                    "runs_listed": len(runs),
                    "status": (
                        "collected" if page_completeness[runs_path] else "partial"
                    ),
                }
            )
        except Exception as exc:
            result["repository_coverage"].append(
                {"repo": repo, "runs_listed": 0, "status": "error"}
            )
            problem("runs", repo, exc)

    correlated = 0
    for key in release_runs:
        if key in selected:
            continue
        if correlated >= max_correlated_runs:
            problem("correlated_run_limit", f"{key[0]}/{key[1]}", warning=True)
            continue
        correlated += 1
        repo, run_id = key
        try:
            selected[key] = get(f"/repos/{repo}/actions/runs/{run_id}")
        except Exception as exc:
            problem("release_run", f"{repo}/{run_id}", exc)

    all_jobs_cache = {}

    def fallback_jobs(repo, run_id, attempt):
        """Recover an unavailable attempt endpoint without mixing attempts.

        GitHub sometimes returns 502 for attempt-specific job listings while
        filter=all works. Cache that bounded listing once per run. Its explicit
        provenance is required for every returned job; absent/foreign metadata
        leaves a coverage gap rather than guessing ownership from timestamps.
        """
        key = (repo, run_id)
        path = f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=all"
        if key not in all_jobs_cache:
            try:
                jobs = pages(path, "jobs", page_size=25)
                all_jobs_cache[key] = (jobs, page_completeness[path], None)
            except Exception as exc:
                all_jobs_cache[key] = ([], False, exc)
        jobs, complete, error = all_jobs_cache[key]
        if error is not None:
            result["job_fallbacks"].append(
                {
                    "repo": repo,
                    "run_id": run_id,
                    "run_attempt": attempt,
                    "source": "all_jobs_filter_all",
                    "complete": False,
                    "jobs": 0,
                    "error": (
                        str(error)
                        if isinstance(error, CollectionError)
                        else type(error).__name__
                    ),
                }
            )
            raise CollectionError("All-attempt job fallback unavailable") from error
        exact = []
        unproven = False
        for job in jobs:
            job_run_id = job.get("run_id")
            job_attempt = job.get("run_attempt")
            if (
                type(job_run_id) is not int
                or job_run_id != run_id
                or type(job_attempt) is not int
                or job_attempt < 1
            ):
                unproven = True
                continue
            if job_attempt == attempt:
                exact.append(job)
        if unproven:
            problem(
                "jobs_fallback_provenance",
                f"{repo}/{run_id}/attempts/{attempt}",
                warning=True,
            )
        complete = complete and not unproven
        result["job_fallbacks"].append(
            {
                "repo": repo,
                "run_id": run_id,
                "run_attempt": attempt,
                "source": "all_jobs_filter_all",
                "complete": complete,
                "jobs": len(exact),
            }
        )
        return exact, complete

    for (repo, run_id), latest in selected.items():
        result["runs"] += 1
        try:
            latest_attempt = int(latest.get("run_attempt") or 1)
            first = max(1, latest_attempt - max_attempts + 1)
            if first > 1:
                problem("attempt_limit", f"{repo}/{run_id}", warning=True)
            for attempt in range(first, latest_attempt + 1):
                identity = f"{repo}/{run_id}/attempts/{attempt}"
                try:
                    run = (
                        latest
                        if attempt == latest_attempt
                        else get(
                            f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}"
                        )
                    )
                    # Never substitute the latest attempt's status for history.
                    run = {**run, "id": run_id, "run_attempt": attempt}
                    jobs_complete = True
                    jobs_source = "attempt"
                    try:
                        jobs_path = f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"
                        raw_jobs = pages(
                            jobs_path,
                            "jobs",
                            page_size=25,
                        )
                        jobs_complete = page_completeness[jobs_path]
                    except Exception as exc:
                        try:
                            raw_jobs, jobs_complete = fallback_jobs(
                                repo, run_id, attempt
                            )
                            jobs_source = "all_jobs_filter_all"
                        except Exception:
                            raw_jobs = []
                            jobs_complete = False
                            problem("jobs", identity, exc)
                    jobs = [_normalize_job(job) for job in raw_jobs]
                    normalized = _normalize_run(
                        repo, run, jobs, release_runs.get((repo, run_id))
                    )
                    normalized["jobs_complete"] = jobs_complete
                    normalized["jobs_source"] = jobs_source
                    normalized["observed_at"] = observed_at
                    store.ingest_run(normalized)
                    result["attempts"] += 1
                    result["jobs"] += len(jobs)
                except Exception as exc:
                    problem("attempt", identity, exc)
            if include_artifacts:
                try:
                    artifacts = pages(
                        f"/repos/{repo}/actions/runs/{run_id}/artifacts",
                        "artifacts",
                        cap=100,
                    )
                    downloaded_artifacts = 0
                    for artifact in artifacts:
                        if artifact.get("expired"):
                            problem(
                                "expired_artifact",
                                f'{repo}/{artifact.get("id")}',
                                warning=True,
                            )
                            continue
                        # Artifacts are run-scoped, not attempt-scoped. Avoid
                        # assigning an earlier attempt's report to a rerun.
                        metadata_attempt = artifact.get("workflow_run", {}).get(
                            "run_attempt"
                        ) or artifact.get("run_attempt")
                        # Producers can explicitly opt in without relying on a
                        # GitHub API field that is absent from most artifacts.
                        # This name is read from trusted GitHub artifact
                        # metadata for this repository/run, never from a ZIP.
                        named_attempt = JUNIT_ARTIFACT_PATTERN.fullmatch(
                            artifact.get("name", "")
                        )
                        if named_attempt:
                            explicit_attempt = int(named_attempt.group(1))
                            if (
                                metadata_attempt is not None
                                and int(metadata_attempt) != explicit_attempt
                            ):
                                problem(
                                    "artifact_attempt_conflict",
                                    f'{repo}/{artifact.get("id")}',
                                    warning=True,
                                )
                                continue
                            metadata_attempt = explicit_attempt
                        if metadata_attempt is None:
                            result["unattributed_artifacts"] += 1
                            continue
                        artifact_attempt = int(metadata_attempt)
                        if not first <= artifact_attempt <= latest_attempt:
                            continue
                        if downloaded_artifacts >= max_artifacts:
                            problem(
                                "artifact_download_limit",
                                f'{repo}/{artifact.get("id")}',
                                warning=True,
                            )
                            continue
                        if artifact.get("size_in_bytes", 0) > max_artifact_bytes:
                            problem(
                                "artifact_size_limit",
                                f'{repo}/{artifact.get("id")}',
                                warning=True,
                            )
                            continue
                        artifact_id = int(artifact["id"])
                        if result["requests"] >= max_requests:
                            raise CollectionError("Collection request budget exhausted")
                        result["requests"] += 1
                        downloaded_artifacts += 1
                        data = client.download_artifact(
                            f"/repos/{repo}/actions/artifacts/{artifact_id}/zip",
                            max_artifact_bytes,
                        )
                        context = {
                            "repo": repo,
                            "run_id": run_id,
                            "run_attempt": artifact_attempt,
                            "source": f"github-artifact:{repo}/{artifact_id}",
                            "observed_at": observed_at,
                        }
                        records = parse_junit_archive(
                            data, context, max_bytes=max_artifact_bytes
                        )
                        if records:
                            store.ingest_tests(records)
                            result["artifact_reports"] += 1
                            result["test_cases"] += len(records)
                except Exception as exc:
                    problem("artifacts", f"{repo}/{run_id}", exc)
        except Exception as exc:
            problem("run", f"{repo}/{run_id}", exc)
    if result["unattributed_artifacts"]:
        problem("artifact_attempt_unproven", "run-scoped artifacts", warning=True)
    if not include_artifacts:
        result["warnings"].append(
            {"stage": "test_coverage", "source": "JUnit artifact collection disabled"}
        )
    if not result["release_documents"] and not result["attempts"]:
        result["status"] = "error"
    result["completed_at"] = _iso(_timestamp()) if now is None else observed_at
    store.record_collection(result)
    return result
