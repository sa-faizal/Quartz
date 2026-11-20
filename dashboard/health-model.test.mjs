// Copyright Advanced Micro Devices, Inc.
// SPDX-License-Identifier: MIT
import test from "node:test";
import assert from "node:assert/strict";
import { sourceURL, evidenceURL, outcome, normalizeFeed, latestRuns, filterRuns, dailyOutcomes, effectiveFreshness, duration, flattenReleasePipelines, releaseEvidenceURL, variantLabel, regressionLabel, healthCounts } from "./health-model.mjs";

test("only HTTPS and local preview sources are accepted; credentials are rejected", () => {
  assert.equal(sourceURL("./health.json", "https://quartz.example/dashboard/"), "https://quartz.example/dashboard/health.json");
  assert.equal(sourceURL("http://localhost:8000/health.json"), "http://localhost:8000/health.json");
  for (const value of ["javascript:alert(1)", "data:application/json,{}", "http://public.example/health.json", "https://token:secret@example.com/health.json"]) assert.throws(() => sourceURL(value));
  assert.equal(evidenceURL("javascript:alert(1)"), null);
  assert.equal(evidenceURL("https://github.com/ROCm/TheRock/actions/runs/1"), "https://github.com/ROCm/TheRock/actions/runs/1");
});

test("retry recovery is based on earlier failure; attempts are not counted as distinct runs", () => {
  const runs = [
    { repo: "ROCm/TheRock", id: 1, run_attempt: 1, status: "completed", conclusion: "failure", created_at: "2026-11-19T01:00:00Z" },
    { repo: "ROCm/TheRock", id: 1, run_attempt: 2, status: "completed", conclusion: "success", created_at: "2026-11-19T01:00:00Z" },
    { repo: "ROCm/Other", id: 1, run_attempt: 2, status: "completed", conclusion: "success", created_at: "2026-11-19T01:00:00Z" },
  ];
  assert.equal(latestRuns(runs).length, 2);
  assert.equal(latestRuns(runs).find((run) => run.repo === "ROCm/TheRock").recovered, true);
  assert.equal(latestRuns(runs).find((run) => run.repo === "ROCm/Other").recovered, false);
  assert.deepEqual(dailyOutcomes(runs), [{ date: "2026-11-19", success: 2, failure: 0, other: 0 }]);
});

test("feed missing arrays are empty, while unsupported or malformed fields fail explicitly", () => {
  assert.deepEqual(normalizeFeed({ schema_version: "1.1" }).tests, []);
  assert.throws(() => normalizeFeed({ schema_version: "2.0" }), /Unsupported/);
  assert.throws(() => normalizeFeed({ schema_version: "1.0", runs: {} }), /runs must be an array/);
  assert.throws(() => normalizeFeed(null), /health document/);
});

test("filters combine repository, outcome, trigger and plain-text search", () => {
  const runs = [{ repo: "ROCm/TheRock", name: "ASAN build", id: 1, status: "completed", conclusion: "failure", event: "push" }, { repo: "ROCm/Quartz", name: "Unit tests", id: 2, status: "completed", conclusion: "success", event: "pull_request" }];
  assert.equal(filterRuns(runs, { repo: "ROCm/TheRock", state: "failure", event: "push", search: "asan" }).length, 1);
  assert.equal(filterRuns(runs, { repo: "ROCm/TheRock", state: "success" }).length, 0);
  assert.equal(filterRuns(runs, { search: "[.*]" }).length, 0);
});

test("freshness ages after publication, and explicitly missing/stale data stays visible", () => {
  const now = Date.parse("2026-11-19T12:00:00Z");
  assert.equal(effectiveFreshness({ freshness: { status: "fresh", latest_observed_at: "2026-11-19T08:00:00Z" } }, now), "stale");
  assert.equal(effectiveFreshness({ freshness: { status: "fresh", latest_observed_at: "2026-11-19T11:59:00Z" } }, now), "fresh");
  assert.equal(effectiveFreshness({ freshness: { status: "missing" } }, now), "missing");
  assert.equal(effectiveFreshness({}), "unknown");
  assert.equal(duration(null), "—");
  assert.equal(duration(125), "2m 5s");
});

test("normalized in-progress outcomes retain their meaning when rendered as badges", () => {
  assert.equal(outcome({ status: "in_progress" }), "running");
  assert.equal(outcome({ status: outcome({ status: "queued" }) }), "running");
});

test("release evidence expands variants without duplicate rollups and preserves inherited provenance", () => {
  const pipelines = {
    pytorch: {
      test: { linux: { gfx942: { status: "failure", run_id: 101, run_attempt: 2, variants: [
        { py: "3.12", torch: "nightly", status: "success" },
        { py: "3.13", torch: "release/2.10", status: "failure", run_id: 102, run_attempt: 3 },
      ] } } },
      build: { windows: { status: "success", run_id: 103, run_attempt: 1 } },
    },
    native_packages: { rpm: { status: "success", run_id: 104, run_attempt: 1 } },
  };
  const rows = flattenReleasePipelines(pipelines, [{ repo: "ROCm/TheRock", id: 102, run_attempt: 3, html_url: "https://github.com/ROCm/TheRock/actions/runs/102" }]);
  assert.equal(rows.length, 4);
  assert.deepEqual(rows[0].variants, { py: "3.12", torch: "nightly" });
  assert.equal(rows[0].run_id, 101);
  assert.equal(rows[0].run_attempt, 2);
  assert.equal(rows[0].arch, "gfx942");
  assert.equal(rows[0].platform, "linux");
  assert.equal(rows[0].html_url, "https://github.com/ROCm/rockrel/actions/runs/101/attempts/2");
  assert.equal(rows[1].html_url, "https://github.com/ROCm/TheRock/actions/runs/102/attempts/3");
  assert.equal(rows[2].arch, "");
  assert.equal(rows[2].platform, "windows");
  assert.equal(rows[3].pipeline, "native_packages");
  assert.equal(rows[3].platform, "linux");
  assert.equal(variantLabel(rows[1].variants), "Python 3.13 · PyTorch release/2.10");
});

test("empty variant detail does not pretend that an aggregate is an individual matrix entry", () => {
  assert.deepEqual(flattenReleasePipelines({ pytorch: { build: { linux: { status: "in_progress", variants: [] } } } }), []);
});

test("workflow evidence uses exact attempts and rejects unsafe repository/source strings", () => {
  assert.equal(releaseEvidenceURL(77, 3, "ROCm/Quartz", "https://github.com/ROCm/Quartz/actions/runs/77/attempts/1"), "https://github.com/ROCm/Quartz/actions/runs/77/attempts/3");
  assert.equal(releaseEvidenceURL(77, 2, "ROCm/Quartz", "javascript:alert(1)"), "https://github.com/ROCm/Quartz/actions/runs/77/attempts/2");
  assert.equal(releaseEvidenceURL(77, 1, "evil.example/a/b"), null);
  assert.equal(releaseEvidenceURL("not-a-run", 1, "ROCm/Quartz"), null);
  assert.equal(flattenReleasePipelines({ rocm: { build: { linux: { status: "success", run_id: 9 } } } }, [], "Example/Release")[0].html_url, "https://github.com/Example/Release/actions/runs/9");
});

test("regressions describe dimensions and version pairs in readable words", () => {
  assert.equal(regressionLabel({ dimension: 'pytorch / test / linux / gfx942 / {"py":"3.12","torch":"release/2.10"}', previous: "success", current: "failure" }), "PyTorch · test · Linux · gfx942 · Python 3.12 · PyTorch release/2.10: passed to failed");
});

test("durable summary totals take precedence over truncated detail lists, including explicit zeros", () => {
  const feed = { summary: { runs: 500, failed_runs: 300, running_runs: 20, recovered_runs: 0, test_cases: 1000, failed_tests: 300 }, runs: [{ repo: "ROCm/TheRock", id: 1, status: "completed", conclusion: "success", recovered: true }], tests: [{ status: "fail" }], export_limits: { truncated_lists: { runs: 500, tests: 1000 }, max_records_per_list: 200 } };
  const counts = healthCounts(feed);
  assert.equal(counts.runs, 500);
  assert.equal(counts.failed_runs, 300);
  assert.equal(counts.running_runs, 20);
  assert.equal(counts.recovered_runs, 0);
  assert.equal(counts.test_cases, 1000);
  assert.equal(counts.failed_tests, 300);
  assert.equal(healthCounts({ tests: [{ status: "fail" }], summary: { test_cases: null, failed_tests: -3 } }).failed_tests, 1);
});
