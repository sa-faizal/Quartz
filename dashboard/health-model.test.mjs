// Copyright Advanced Micro Devices, Inc.
// SPDX-License-Identifier: MIT
import test from "node:test";
import assert from "node:assert/strict";
import { sourceURL, evidenceURL, outcome, normalizeFeed, latestRuns, filterRuns, dailyOutcomes, effectiveFreshness, duration } from "./health-model.mjs";

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
