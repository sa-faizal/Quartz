// Copyright Advanced Micro Devices, Inc.
// SPDX-License-Identifier: MIT

export const DEFAULT_SOURCE = "https://raw.githubusercontent.com/sa-faizal/Quartz/ci-health-data/health.json";

export function sourceURL(value, base) {
  const url = new URL(value, base);
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (url.username || url.password || (url.protocol !== "https:" && !(url.protocol === "http:" && local))) {
    throw new Error("Use an HTTPS feed URL, or HTTP on localhost for local previews.");
  }
  url.hash = "";
  return url.href;
}

export function evidenceURL(value, base) {
  if (!value) return null;
  try {
    const url = new URL(value, base);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function outcome(item) {
  if (!item) return "unknown";
  if (["queued", "in_progress", "waiting", "pending", "requested", "running"].includes(item.status)) return "running";
  const state = item.conclusion || item.status || "unknown";
  if (["failure", "fail", "error", "timed_out", "startup_failure", "action_required"].includes(state)) return "failure";
  if (["success", "pass"].includes(state)) return "success";
  return ["cancelled", "skipped", "neutral"].includes(state) ? state : "unknown";
}

export function latestRuns(runs = []) {
  const grouped = new Map();
  for (const run of runs) {
    const key = `${run.repo || ""}/${run.id ?? run.run_id}`;
    const previous = grouped.get(key);
    if (!previous || Number(run.run_attempt || 1) > Number(previous.run_attempt || 1)) grouped.set(key, run);
  }
  return [...grouped.values()].map((run) => {
    const priorFailure = runs.some((candidate) => candidate.repo === run.repo && (candidate.id ?? candidate.run_id) === (run.id ?? run.run_id) && Number(candidate.run_attempt || 1) < Number(run.run_attempt || 1) && outcome(candidate) === "failure");
    return { ...run, recovered: run.recovered === true || (outcome(run) === "success" && priorFailure) };
  }).sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
}

export function normalizeFeed(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("The source did not return a Quartz health document.");
  if (String(data.schema_version || "").split(".")[0] !== "1") throw new Error(`Unsupported health feed schema: ${String(data.schema_version || "missing")}. Expected major version 1.`);
  for (const key of ["releases", "runs", "tests", "downstream", "gaps"]) {
    if (data[key] !== undefined && !Array.isArray(data[key])) throw new Error(`Invalid health feed: ${key} must be an array.`);
  }
  const arrays = Object.fromEntries(["releases", "runs", "tests", "downstream", "gaps"].map((key) => [key, data[key] || []]));
  return { ...data, ...arrays, currentRuns: latestRuns(arrays.runs) };
}

export function filterRuns(runs, { repo = "", state = "", event = "", search = "" } = {}) {
  const query = search.trim().toLowerCase();
  return runs.filter((run) => (!repo || run.repo === repo) && (!state || (state === "recovered" ? run.recovered === true : outcome(run) === state)) && (!event || run.event === event) && (!query || [run.name, run.branch, run.head_sha, run.repo, run.id ?? run.run_id].join(" ").toLowerCase().includes(query)));
}

export function dailyOutcomes(runs) {
  const days = new Map();
  for (const run of latestRuns(runs)) {
    if (!/^\d{4}-\d{2}-\d{2}T/.test(String(run.created_at || ""))) continue;
    const day = run.created_at.slice(0, 10);
    const counts = days.get(day) || { date: day, success: 0, failure: 0, other: 0 };
    const state = outcome(run);
    counts[["success", "failure"].includes(state) ? state : "other"]++;
    days.set(day, counts);
  }
  return [...days.values()].sort((a, b) => a.date.localeCompare(b.date));
}

export function relativeAge(value, now = Date.now()) {
  const seconds = Math.max(0, Math.floor((now - Date.parse(value)) / 1000));
  if (!Number.isFinite(seconds)) return "Unknown";
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} hr ago`;
  return `${Math.floor(seconds / 86400)} days ago`;
}

export function effectiveFreshness(feed, now = Date.now()) {
  if (feed.freshness?.status === "missing") return "missing";
  const value = feed.freshness?.latest_observed_at || feed.generated_at;
  const age = (now - Date.parse(value)) / 1000;
  if (!Number.isFinite(age)) return "unknown";
  return feed.freshness?.status === "stale" || age > (Number(feed.freshness?.stale_after_seconds) || 7200) ? "stale" : "fresh";
}

export function duration(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const seconds = Math.max(0, Math.round(Number(value)));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
}

const LEAF_METADATA = new Set(["status", "run_id", "run_attempt", "started_at", "completed_at", "created_at", "updated_at", "observed_at", "html_url", "repo", "conclusion"]);
const DIMENSION_LABELS = { py: "Python", python: "Python", torch: "PyTorch", jax: "JAX", test_size: "Test size" };

export function variantLabel(variants = {}) {
  return Object.entries(variants).map(([key, value]) => `${DIMENSION_LABELS[key] || key.replaceAll("_", " ")} ${String(value)}`).join(" · ");
}

export function releaseEvidenceURL(runId, attempt, repo, sourceURLValue) {
  if (!Number.isSafeInteger(Number(runId)) || Number(runId) <= 0) return null;
  const validAttempt = Number.isSafeInteger(Number(attempt)) && Number(attempt) > 0;
  const supplied = evidenceURL(sourceURLValue);
  if (supplied) {
    const url = new URL(supplied);
    // GitHub attempt URLs identify the exact execution instead of its moving latest result.
    if (url.hostname === "github.com" && /\/actions\/runs\/\d+(?:\/attempts\/\d+)?\/?$/.test(url.pathname) && validAttempt) {
      url.pathname = url.pathname.replace(/(?:\/attempts\/\d+)?\/?$/, `/attempts/${Number(attempt)}`);
    }
    return url.href;
  }
  if (typeof repo !== "string" || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) return null;
  return `https://github.com/${repo}/actions/runs/${Number(runId)}${validAttempt ? `/attempts/${Number(attempt)}` : ""}`;
}

export function flattenReleasePipelines(pipelines, runs = [], releaseRepo = "ROCm/rockrel") {
  const rows = [];
  const runIndex = new Map();
  for (const run of runs) {
    const key = String(run.id ?? run.run_id);
    if (!runIndex.has(key)) runIndex.set(key, []);
    runIndex.get(key).push(run);
  }
  function walk(value, path, inherited = {}, variants = {}, depth = 0) {
    if (!value || typeof value !== "object" || Array.isArray(value) || depth > 30) return;
    const provenance = { ...inherited };
    for (const key of ["run_id", "run_attempt", "started_at", "completed_at"]) if (value[key] !== undefined && value[key] !== null) provenance[key] = value[key];
    const hasVariants = Array.isArray(value.variants);
    const childObjects = Object.entries(value).filter(([key, child]) => key !== "variants" && child && typeof child === "object" && !Array.isArray(child));
    if (typeof value.status === "string" && !hasVariants && !childObjects.length) {
      const matches = runIndex.get(String(provenance.run_id)) || [];
      const matchingAttempt = matches.filter((run) => Number(run.run_attempt || 1) === Number(provenance.run_attempt || 1));
      const candidates = matchingAttempt.length ? matchingAttempt : matches;
      const matched = candidates.find((run) => run.repo === releaseRepo) || candidates[0];
      const repo = matched?.repo || releaseRepo;
      const platform = path.find((part) => part === "linux" || part === "windows") || (path[0] === "native_packages" ? "linux" : "");
      const arch = path.find((part) => /^gfx/i.test(part)) || "";
      rows.push({ pipeline: path[0] || "", phase: path[1] || "", platform, arch, variants, status: value.status, ...provenance, repo, html_url: releaseEvidenceURL(provenance.run_id, provenance.run_attempt, repo, matched?.html_url) });
    }
    if (hasVariants) {
      for (const variant of value.variants) {
        if (!variant || typeof variant !== "object" || Array.isArray(variant)) continue;
        const dimensions = Object.fromEntries(Object.entries(variant).filter(([key, child]) => !LEAF_METADATA.has(key) && child !== null && typeof child !== "object"));
        walk(variant, path, provenance, { ...variants, ...dimensions }, depth + 1);
      }
    }
    for (const [key, child] of childObjects) walk(child, [...path, key], provenance, variants, depth + 1);
  }
  walk(pipelines, []);
  return rows;
}

export function regressionLabel(regression) {
  if (typeof regression === "string") return regression;
  if (!regression || typeof regression !== "object") return "A newly failing matrix entry was reported.";
  const dimension = String(regression.dimension || "Matrix entry").split(" / ").map((part) => {
    if (part.startsWith("{")) {
      try { return variantLabel(JSON.parse(part)); } catch { return part; }
    }
    return ({ rocm: "ROCm", pytorch: "PyTorch", jax: "JAX", linux: "Linux", windows: "Windows", test_full: "full-suite test", native_packages: "native packages" })[part] || part.replaceAll("_", " ");
  }).filter(Boolean).join(" · ");
  const state = { success: "passed", failure: "failed", in_progress: "in progress", cancelled: "cancelled", skipped: "skipped" };
  return `${dimension}: ${state[regression.previous] || regression.previous || "previous outcome unavailable"} to ${state[regression.current] || regression.current || "current outcome unavailable"}`;
}

export function healthCounts(feed) {
  const runs = feed.currentRuns || latestRuns(feed.runs || []);
  const tests = feed.tests || [];
  const fallback = {
    releases: (feed.releases || []).length,
    runs: runs.length,
    failed_runs: runs.filter((run) => outcome(run) === "failure").length,
    running_runs: runs.filter((run) => outcome(run) === "running").length,
    recovered_runs: runs.filter((run) => run.recovered).length,
    test_cases: tests.length,
    failed_tests: tests.filter((test) => ["fail", "failure", "error"].includes(test.status)).length,
    downstream_runs: (feed.downstream || []).length,
  };
  return Object.fromEntries(Object.entries(fallback).map(([key, value]) => {
    const supplied = feed.summary?.[key];
    return [key, supplied !== null && supplied !== undefined && Number.isSafeInteger(Number(supplied)) && Number(supplied) >= 0 ? Number(supplied) : value];
  }));
}
