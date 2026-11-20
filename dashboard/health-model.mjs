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
