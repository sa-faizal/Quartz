# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

import io
import sys
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ci_health.collect import (
    CollectionError,
    _StripAuthRedirect,
    _history_sources,
    collect,
)
from urllib.request import Request


NOW = "2026-11-19T12:00:00Z"


class Store:
    def __init__(self):
        self.releases, self.runs, self.tests, self.collections = [], [], [], []

    def ingest_release(self, document, source, observed_at):
        self.releases.append(document)
        return "release:1"

    def ingest_run(self, record):
        self.runs.append(record)

    def ingest_tests(self, records):
        self.tests.extend(records)

    def record_collection(self, record):
        self.collections.append(record)


def run(run_id=10, attempt=1, conclusion="success"):
    return {
        "id": run_id,
        "run_attempt": attempt,
        "name": "Test ASAN",
        "event": "pull_request",
        "head_branch": "feature",
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": "2026-11-19T01:00:00Z",
        "updated_at": "2026-11-19T02:00:00Z",
    }


def test_paginated_jobs_and_exact_attempt_history():
    class Client:
        def get(self, path):
            if "/jobs?" in path:
                if parse_qs(urlsplit(path).query)["page"] == ["2"]:
                    return {"jobs": [{"id": 101, "name": "last"}]}
                return {
                    "jobs": [
                        {"id": i, "name": f"job{i}", "conclusion": "success"}
                        for i in range(25)
                    ]
                }
            if "/attempts/1" in path:
                return run(attempt=1, conclusion="failure")
            return {"workflow_runs": [run(attempt=2)], "total_count": 1}

    store = Store()
    outcome = collect(
        store, repos=["ROCm/TheRock"], release_sources=[], now=NOW, client=Client()
    )
    assert outcome["status"] == "success"
    assert outcome["jobs"] == 52
    assert [record["run_attempt"] for record in store.runs] == [1, 2]
    assert [record["conclusion"] for record in store.runs] == ["failure", "success"]
    assert store.runs[1]["event"] == "pull_request"
    assert store.runs[1]["completed_at"] == "2026-11-19T02:00:00Z"


@pytest.mark.parametrize("total", [100, 103])
@pytest.mark.parametrize("fallback", [False, True])
def test_job_page_size_25_recovers_large_run_and_stops_at_total(total, fallback):
    class Client:
        def __init__(self):
            self.job_pages = []

        def get(self, path):
            if "/jobs?" in path:
                if fallback and "filter=all" not in path:
                    raise CollectionError("GitHub HTTP 502")
                query = parse_qs(urlsplit(path).query)
                assert query["per_page"] == ["25"]
                page = int(query["page"][0])
                self.job_pages.append(page)
                begin = (page - 1) * 25
                return {
                    "total_count": total,
                    "jobs": [
                        {"id": i, "run_id": 10, "run_attempt": 1}
                        for i in range(begin, min(begin + 25, total))
                    ],
                }
            return {"workflow_runs": [run()], "total_count": 1}

    client = Client()
    store = Store()
    outcome = collect(
        store, repos=["ROCm/rockrel"], release_sources=[], now=NOW, client=client
    )
    assert outcome["status"] == "success"
    assert len(store.runs[0]["jobs"]) == total
    assert store.runs[0]["jobs_complete"] is True
    assert client.job_pages == list(range(1, (total + 24) // 25 + 1))


def test_502_attempt_jobs_fallback_is_cached_and_never_mixes_attempts():
    class Client:
        def __init__(self):
            self.all_job_calls = 0

        def get(self, path):
            if "jobs?filter=all" in path:
                self.all_job_calls += 1
                return {
                    "jobs": [
                        {"id": 101, "name": "old", "run_id": 10, "run_attempt": 1},
                        {"id": 102, "name": "retry", "run_id": 10, "run_attempt": 2},
                        {"id": 103, "name": "last", "run_id": 10, "run_attempt": 3},
                    ]
                }
            if "/jobs?" in path:
                if "/attempts/1/" in path:
                    return {"jobs": [{"id": 101, "name": "old"}]}
                raise CollectionError("GitHub HTTP 502")
            if "/attempts/" in path:
                return run(attempt=int(path.rsplit("/", 1)[-1]), conclusion="failure")
            return {"workflow_runs": [run(attempt=3)]}

    client = Client()
    store = Store()
    outcome = collect(
        store, repos=["ROCm/rockrel"], release_sources=[], now=NOW, client=client
    )
    assert outcome["status"] == "success"
    assert outcome["errors"] == []
    assert client.all_job_calls == 1
    assert [[job["id"] for job in record["jobs"]] for record in store.runs] == [
        [101],
        [102],
        [103],
    ]
    assert [record["jobs_source"] for record in store.runs] == [
        "attempt",
        "all_jobs_filter_all",
        "all_jobs_filter_all",
    ]
    assert all(record["jobs_complete"] for record in store.runs)
    assert [item["run_attempt"] for item in outcome["job_fallbacks"]] == [2, 3]


def test_jobs_fallback_paginates_all_attempts():
    class Client:
        def get(self, path):
            if "jobs?filter=all" in path:
                if parse_qs(urlsplit(path).query)["page"] == ["2"]:
                    return {"jobs": [{"id": 100, "run_id": 10, "run_attempt": 1}]}
                return {
                    "jobs": [
                        {"id": i, "run_id": 10, "run_attempt": (i % 2) + 1}
                        for i in range(25)
                    ]
                }
            if "/jobs?" in path:
                raise CollectionError("GitHub HTTP 502")
            return {"workflow_runs": [run()]}

    store = Store()
    outcome = collect(
        store, repos=["ROCm/rockrel"], release_sources=[], now=NOW, client=Client()
    )
    assert outcome["status"] == "success"
    assert len(store.runs[0]["jobs"]) == 14
    assert store.runs[0]["jobs_complete"] is True


def test_jobs_fallback_requires_explicit_matching_run_and_attempt():
    class Client:
        def get(self, path):
            if "jobs?filter=all" in path:
                return {
                    "jobs": [
                        {"id": 1, "run_id": 10, "run_attempt": 1},
                        {"id": 2, "run_id": 10},
                        {"id": 3, "run_id": 11, "run_attempt": 1},
                        {"id": 4, "run_id": 10, "run_attempt": 2},
                    ]
                }
            if "/jobs?" in path:
                raise CollectionError("GitHub HTTP 502")
            return {"workflow_runs": [run()]}

    store = Store()
    outcome = collect(
        store, repos=["ROCm/rockrel"], release_sources=[], now=NOW, client=Client()
    )
    assert outcome["status"] == "partial"
    assert [job["id"] for job in store.runs[0]["jobs"]] == [1]
    assert store.runs[0]["jobs_complete"] is False
    assert any(w["stage"] == "jobs_fallback_provenance" for w in outcome["warnings"])


def test_jobs_fallback_request_budget_and_failed_cache_are_bounded():
    class Client:
        def __init__(self):
            self.all_job_calls = 0

        def get(self, path):
            if "jobs?filter=all" in path:
                self.all_job_calls += 1
                return {
                    "jobs": [
                        {"id": i, "run_id": 10, "run_attempt": 1} for i in range(25)
                    ]
                }
            if "/jobs?" in path:
                raise CollectionError("GitHub HTTP 502")
            return {"workflow_runs": [run()]}

    client = Client()
    store = Store()
    outcome = collect(
        store,
        repos=["ROCm/rockrel"],
        release_sources=[],
        now=NOW,
        client=client,
        max_requests=3,
    )
    assert outcome["status"] == "partial"
    assert outcome["requests"] == 3
    assert client.all_job_calls == 1
    assert store.runs[0]["jobs_complete"] is False
    assert outcome["job_fallbacks"][0]["complete"] is False
    assert outcome["job_fallbacks"][0]["error"] == "Collection request budget exhausted"


def test_failed_jobs_fallback_is_not_repeated_for_later_attempts():
    class Client:
        def __init__(self):
            self.all_job_calls = 0

        def get(self, path):
            if "jobs?filter=all" in path:
                self.all_job_calls += 1
                raise CollectionError("GitHub HTTP 502")
            if "/jobs?" in path:
                raise CollectionError("GitHub HTTP 502")
            if "/attempts/" in path:
                return run(attempt=1, conclusion="failure")
            return {"workflow_runs": [run(attempt=2)]}

    client = Client()
    store = Store()
    outcome = collect(
        store, repos=["ROCm/rockrel"], release_sources=[], now=NOW, client=client
    )
    assert client.all_job_calls == 1
    assert outcome["status"] == "partial"
    assert len(outcome["job_fallbacks"]) == 2
    assert all(record["jobs_complete"] is False for record in store.runs)


def test_partial_jobs_error_retains_failure_and_other_repo_collects():
    class Client:
        def get(self, path):
            if "broken/repo" in path:
                raise CollectionError("GitHub HTTP 403")
            if "/jobs?" in path:
                raise CollectionError("GitHub HTTP 404")
            return {"workflow_runs": [run(conclusion="failure")]}

    store = Store()
    result = collect(
        store,
        repos=["broken/repo", "ROCm/TheRock"],
        release_sources=[],
        now=NOW,
        client=Client(),
    )
    assert result["status"] == "partial"
    assert len(result["errors"]) == 2
    assert store.runs[0]["conclusion"] == "failure"
    assert store.runs[0]["jobs_complete"] is False
    assert store.collections == [result]


def test_run_limit_and_request_budget_explicit():
    class Client:
        def get(self, path):
            return {"workflow_runs": [run(10), run(11)], "total_count": 2}

    store = Store()
    result = collect(
        store,
        repos=["ROCm/TheRock"],
        release_sources=[],
        now=NOW,
        max_runs=1,
        max_requests=1,
        client=Client(),
    )
    assert result["requests"] == 1
    assert result["status"] == "partial"
    assert any(w["stage"] == "limit" for w in result["warnings"])
    assert store.runs[0]["jobs_complete"] is False


def test_job_pagination_limit_is_not_reported_as_complete():
    class Client:
        def get(self, path):
            if "/jobs?" in path:
                return {"jobs": [{"id": i, "name": "job"} for i in range(25)]}
            return {"workflow_runs": [run()]}

    store = Store()
    result = collect(
        store,
        repos=["ROCm/TheRock"],
        release_sources=[],
        now=NOW,
        client=Client(),
    )
    assert result["status"] == "partial"
    assert store.runs[0]["jobs_complete"] is False
    assert any(w["stage"] == "pagination_limit" for w in result["warnings"])


def test_release_correlated_runs_and_bounds():
    class Client:
        def get(self, path):
            if "/jobs?" in path:
                return {"jobs": []}
            if "/actions/runs/" in path:
                return run(int(path.rsplit("/", 1)[-1]))
            return {"workflow_runs": []}

    store = Store()
    document = {
        "trigger_workflow_run_id": 15,
        "pipelines": {"rocm": {"test": {"run_id": 16}}},
    }
    result = collect(
        store,
        repos=["ROCm/TheRock"],
        release_sources=["https://raw.example/latest.json"],
        release_loader=lambda source: document,
        now=NOW,
        client=Client(),
        max_correlated_runs=1,
    )
    assert result["status"] == "partial"
    assert store.runs[0]["repo"] == "ROCm/rockrel"
    assert store.runs[0]["id"] == 15
    assert store.runs[0]["release_id"] == "release:1"
    assert any(w["stage"] == "correlated_run_limit" for w in result["warnings"])


def test_nightly_history_imports_snapshots_without_correlated_run_flood():
    source = "https://raw.githubusercontent.com/ROCm/Quartz/main/nightly/latest.json"
    fetched = []

    def loader(url):
        fetched.append(url)
        if url == source:
            date, run_id = "20261119", 10
        elif "latest_good" in url:
            date, run_id = "20261116", 99
        else:
            date, run_id = url.split("/")[-2], 99
        return {
            "release_type": "nightly",
            "build_date": date,
            "rocm_version": f"10.3.0a{date}",
            "trigger_workflow_run_id": run_id,
        }

    class Client:
        def get(self, path):
            assert "/runs/99" not in path
            if "/jobs?" in path:
                return {"jobs": []}
            return run(10)

    store = Store()
    result = collect(
        store,
        repos=[],
        release_sources=[source],
        release_loader=loader,
        client=Client(),
        history_days=2,
        now=NOW,
    )
    assert result["status"] == "success"
    assert result["history_documents"] == 3
    assert result["release_documents"] == 4
    assert len(store.releases) == 4
    assert [record["id"] for record in store.runs] == [10]
    assert fetched == [
        source,
        "https://raw.githubusercontent.com/ROCm/Quartz/main/nightly/20261118/status.json",
        "https://raw.githubusercontent.com/ROCm/Quartz/main/nightly/20261117/status.json",
        "https://raw.githubusercontent.com/ROCm/Quartz/main/nightly/latest_good.json",
    ]


def test_missing_history_date_is_explicit_and_does_not_lose_latest_snapshot():
    source = "https://raw.githubusercontent.com/ROCm/Quartz/main/nightly/latest.json"

    def loader(url):
        if url != source:
            raise OSError("not found")
        return {"release_type": "nightly", "build_date": "20261119"}

    store = Store()
    result = collect(
        store,
        repos=[],
        release_sources=[source],
        release_loader=loader,
        history_days=1,
        now=NOW,
    )
    assert result["status"] == "partial"
    assert result["release_documents"] == 1
    assert result["history_documents"] == 0
    assert len(result["warnings"]) >= 2
    assert all(w["stage"] == "history" for w in result["warnings"][:2])


@pytest.mark.parametrize(
    "source",
    [
        "https://evil.example/ROCm/Quartz/main/nightly/latest.json",
        "https://raw.githubusercontent.com/ROCm/Quartz/../nightly/latest.json",
        "https://raw.githubusercontent.com/ROCm/Quartz/main/nightly/latest.json?token=secret",
        "https://raw.githubusercontent.com/ROCm/Quartz/main/nightly/unknown.json",
    ],
)
def test_history_rejects_ambiguous_or_unsafe_source(source):
    with pytest.raises(ValueError):
        _history_sources(
            source, {"release_type": "nightly", "build_date": "20261119"}, 2
        )


def test_history_preserves_variant_and_ignores_non_nightly_streams():
    source = "https://raw.githubusercontent.com/ROCm/Quartz/main/nightly/latest_asan-debug.json"
    derived = _history_sources(
        source, {"release_type": "nightly", "build_date": "20261119"}, 1
    )
    assert derived[0].endswith("/20261118/status_asan-debug.json")
    assert derived[1].endswith("/latest_good_asan-debug.json")
    assert _history_sources(source, {"release_type": "nightly-bkc"}, 7) == []


def test_artifact_without_proven_attempt_is_not_misattributed():
    class Client:
        def get(self, path):
            if "/jobs?" in path:
                return {"jobs": []}
            if "/artifacts?" in path:
                return {"artifacts": [{"id": 99, "name": "junit"}]}
            return {"workflow_runs": [run()]}

        def download_artifact(self, *args):
            raise AssertionError("Unattributed evidence must not be downloaded")

    store = Store()
    result = collect(
        store,
        repos=["ROCm/TheRock"],
        release_sources=[],
        now=NOW,
        client=Client(),
        include_artifacts=True,
    )
    assert result["unattributed_artifacts"] == 1
    assert result["status"] == "partial"
    assert not store.tests


def test_attributed_artifact_junit_ingestion():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "junit.xml",
            '<testsuite><testcase name="a"><failure/></testcase></testsuite>',
        )

    class Client:
        def get(self, path):
            if "/jobs?" in path:
                return {"jobs": []}
            if "/artifacts?" in path:
                return {"artifacts": [{"id": 99, "workflow_run": {"run_attempt": 1}}]}
            return {"workflow_runs": [run()]}

        def download_artifact(self, path, max_bytes):
            return output.getvalue()

    store = Store()
    result = collect(
        store,
        repos=["ROCm/TheRock"],
        release_sources=[],
        now=NOW,
        client=Client(),
        include_artifacts=True,
    )
    assert result["test_cases"] == 1
    assert store.tests[0]["run_attempt"] == 1
    assert store.tests[0]["status"] == "fail"


@pytest.mark.parametrize(
    "name",
    ["quartz-junit-attempt-1", "quartz-junit-attempt-1-linux-gfx942-py3.12"],
)
def test_producer_named_artifact_proves_attempt_without_api_attempt_field(name):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("report.xml", '<testsuite><testcase name="x"/></testsuite>')

    class Client:
        def get(self, path):
            if "/jobs?" in path:
                return {"jobs": []}
            if "/artifacts?" in path:
                return {
                    "artifacts": [{"id": i, "name": f"ordinary-{i}"} for i in range(4)]
                    + [{"id": 99, "name": name}]
                }
            return {"workflow_runs": [run()]}

        def download_artifact(self, path, max_bytes):
            return output.getvalue()

    store = Store()
    result = collect(
        store,
        repos=["ROCm/TheRock"],
        release_sources=[],
        now=NOW,
        client=Client(),
        include_artifacts=True,
        max_artifacts=1,
    )
    assert result["test_cases"] == 1
    assert store.tests[0]["run_attempt"] == 1
    assert result["unattributed_artifacts"] == 4


@pytest.mark.parametrize(
    "name,metadata,stage",
    [
        ("quartz-junit-attempt-1-linux", 2, "artifact_attempt_conflict"),
        ("quartz-junit-attempt-1-../../report", None, "artifact_attempt_unproven"),
        ("quartz-junit-attempt-01-linux", None, "artifact_attempt_unproven"),
    ],
)
def test_artifact_name_conflict_or_unsafe_suffix_is_not_ingested(name, metadata, stage):
    class Client:
        def get(self, path):
            if "/jobs?" in path:
                return {"jobs": []}
            if "/artifacts?" in path:
                artifact = {"id": 99, "name": name}
                if metadata is not None:
                    artifact["workflow_run"] = {"run_attempt": metadata}
                return {"artifacts": [artifact]}
            return {"workflow_runs": [run()]}

        def download_artifact(self, *args):
            raise AssertionError("Unproven/conflicting artifact was downloaded")

    store = Store()
    result = collect(
        store,
        repos=["ROCm/TheRock"],
        release_sources=[],
        now=NOW,
        client=Client(),
        include_artifacts=True,
    )
    assert any(warning["stage"] == stage for warning in result["warnings"])
    assert not store.tests


def test_redirect_clears_auth_and_rejects_http():
    redirector = _StripAuthRedirect()
    request = Request(
        "https://api.github.com/repos/r/o/artifact/zip",
        headers={"Authorization": "Bearer private"},
    )
    redirected = redirector.redirect_request(
        request, None, 302, "found", {}, "https://signed.example/archive.zip"
    )
    assert redirected.get_header("Authorization") is None
    try:
        redirector.redirect_request(
            request, None, 302, "found", {}, "http://signed.example/archive.zip"
        )
    except CollectionError:
        pass
    else:
        raise AssertionError("Insecure redirect accepted")
