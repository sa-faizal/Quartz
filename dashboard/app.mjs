// Copyright Advanced Micro Devices, Inc.
// SPDX-License-Identifier: MIT
import { DEFAULT_SOURCE, sourceURL, evidenceURL, outcome, normalizeFeed, filterRuns, dailyOutcomes, relativeAge, effectiveFreshness, duration } from "./health-model.mjs";

const $ = (id) => document.getElementById(id);
const labels = { success: "Passed", failure: "Failed", running: "In progress", cancelled: "Cancelled", skipped: "Skipped", neutral: "Neutral", unknown: "Not reported", recovered: "Recovered on retry" };
let feed = null;
let visibleRuns = 75;
let visibleReleases = 4;
let activeRequest = null;
let activeSource = DEFAULT_SOURCE;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}
function append(parent, ...children) { parent.append(...children.filter(Boolean)); return parent; }
function replace(id, ...children) { $(id).replaceChildren(...children.filter(Boolean)); }
function link(text, target, className = "") {
  const url = evidenceURL(target, activeSource);
  if (!url) return node("span", className, text);
  const anchor = node("a", className, text);
  anchor.href = url;
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  return anchor;
}
function badge(state, override) {
  const value = state === "recovered" ? state : outcome({ status: state });
  return node("span", `status ${value}`, override || labels[value]);
}
function empty(title, description, compact = false) { return append(node("div", compact ? "empty compact" : "empty"), node("strong", "", title), node("p", "", description)); }
function timestamp(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "Not reported";
}
function number(value) { return Number.isFinite(Number(value)) ? Number(value).toLocaleString() : "—"; }
function objectText(value) { return typeof value === "string" ? value : JSON.stringify(value); }

function metric(label, value, note, tone = "") {
  return append(node("article", `metric-card ${tone}`), node("div", "metric-label", label), node("div", "metric-value", value), node("div", "metric-note", note));
}
function showFeedState(state, title, detail) {
  $("feed-state").className = `feed-state ${state}`;
  $("feed-state").replaceChildren(node("div", "state-icon", state === "fresh" ? "✓" : state === "loading" ? "◷" : "!"), append(node("div"), node("strong", "", title), node("p", "", detail)));
}
function renderFreshness() {
  if (!feed) return;
  const state = effectiveFreshness(feed);
  const partial = ["partial", "error", "failed", "failure"].includes(feed.collection?.status);
  const observed = feed.freshness?.latest_observed_at || feed.generated_at;
  const sourceTime = `${relativeAge(observed)} · observed ${timestamp(observed)} · times displayed in your timezone`;
  if (state === "stale") showFeedState("stale", partial ? "Collection is incomplete — retained results may be stale" : "Collection is stale — results may have changed", `${sourceTime}${feed.freshness?.reason ? `. ${feed.freshness.reason}` : ""}`);
  else if (state === "missing" || state === "unknown") showFeedState("missing", "Source freshness is not established", "The feed has no usable observation timestamp. Treat these results as incomplete evidence.");
  else if (partial) showFeedState("partial", "Quartz connected · collection has gaps", `${sourceTime}. See collection and coverage for unavailable sources.`);
  else showFeedState("fresh", "Quartz connected · collection is current", sourceTime);
  $("source-status").textContent = `${state === "fresh" ? "Current" : state === "stale" ? "Stale" : "Unknown freshness"} · schema ${feed.schema_version}`;
}

function renderOverview() {
  const runs = feed.currentRuns;
  const failed = runs.filter((run) => outcome(run) === "failure").length;
  const running = runs.filter((run) => outcome(run) === "running").length;
  const recovered = runs.filter((run) => run.recovered).length;
  const builds = feed.releases.flatMap((release) => release.platforms || []);
  const buildReady = builds.filter((platform) => outcome({ status: platform.build_status }) === "success").length;
  const failures = feed.tests.filter((test) => ["fail", "failure", "error"].includes(test.status));
  replace("metrics",
    metric("ROCm builds available", builds.length ? number(buildReady) : "—", builds.length ? `of ${number(builds.length)} release/platform records` : "No release builds collected", "positive"),
    metric("Failed workflow runs", number(failed), `${number(runs.length)} runs · latest attempts`, failed ? "danger" : ""),
    metric("Work in progress", number(running), "Queued or executing workflows", "neutral"),
    metric("Recovered on retry", number(recovered), "Earlier failure observed · not proof of flakiness", "neutral"),
    metric("Failing test cases", feed.tests.length ? number(failures.length) : "—", feed.tests.length ? `${number(feed.tests.length)} reported JUnit cases` : "Individual test evidence not collected", failures.length ? "danger" : ""));
  renderTrend();
  const attention = [];
  if (failed) attention.push(attentionItem(number(failed), "Workflow runs need triage", "Expand a failed run to see its jobs and source evidence.", "#workflow-runs"));
  if (failures.length) attention.push(attentionItem(number(failures.length), "Individual test failures have evidence", "Review the collected assertion or error messages.", "#test-evidence"));
  const failedPlatforms = builds.filter((platform) => Object.values(platform.test_counts || {}).some((value) => Number(value) > 0) && Number(platform.test_counts?.failure) > 0);
  if (failedPlatforms.length) attention.push(attentionItem(number(failedPlatforms.length), "Release platforms have ROCm validation failures", "A successful build can still have failing tests.", "#releases"));
  if (!feed.tests.length) attention.push(attentionItem("—", "Individual test evidence is missing", "Matrix pass counts do not establish individual test-case health.", "#coverage"));
  if (!attention.length) attention.push(empty("No failing evidence in this collection", "This describes the collected window. Check coverage before drawing a broader health conclusion.", true));
  replace("attention", ...attention.slice(0, 3));
}
function attentionItem(count, title, description, href) {
  const anchor = node("a", "attention-item");
  anchor.href = href;
  append(anchor, node("span", "attention-number", count), append(node("div"), node("strong", "", title), node("p", "", description)));
  return anchor;
}
function renderTrend() {
  const days = dailyOutcomes(feed.currentRuns);
  if (days.length < 2) {
    replace("trend-chart", empty("A trend needs more history", "At least two collected dates are needed. Quartz will retain the evidence as collection continues."));
    return;
  }
  const displayed = days.slice(-30);
  const maximum = Math.max(...displayed.map((day) => day.success + day.failure + day.other), 1);
  const chart = node("div", "chart-area");
  chart.setAttribute("role", "img");
  chart.setAttribute("aria-label", `Workflow outcomes by creation date: ${displayed.map((day) => `${day.date}: ${day.success} succeeded, ${day.failure} failed, ${day.other} other`).join("; ")}`);
  for (const [index, day] of displayed.entries()) {
    const total = day.success + day.failure + day.other;
    const column = node("div", "chart-day");
    column.title = `${day.date}: ${day.success} success, ${day.failure} failure, ${day.other} other`;
    const bars = node("div", "chart-bars");
    bars.style.height = `${Math.max(1, total / maximum * 85)}%`;
    for (const state of ["success", "failure", "other"]) {
      if (!day[state]) continue;
      const segment = node("div", `chart-segment ${state}`);
      segment.style.height = `${day[state] / total * 100}%`;
      bars.append(segment);
    }
    append(column, bars, node("span", "chart-label", displayed.length <= 14 || index % 4 === 0 || index === displayed.length - 1 ? day.date.slice(5) : ""));
    chart.append(column);
  }
  replace("trend-chart", chart, node("div", "chart-caption", `UTC creation dates · ${displayed[0].date} to ${displayed.at(-1).date} · omitted dates mean no collected records, not zero CI activity`));
}

function renderReleases() {
  $("release-count").textContent = `${number(feed.releases.length)} collected releases`;
  if (!feed.releases.length) {
    replace("release-list", append(node("div", "panel"), empty("No release status collected", "Configure Quartz release sources in the collector. Workflow results can still appear below.")));
    return;
  }
  const releases = [...feed.releases].sort((a, b) => String(b.build_date || "").localeCompare(String(a.build_date || "")));
  const cards = releases.slice(0, visibleReleases).map((release) => {
    const card = node("article", "release-card");
    const identity = append(node("div", "release-title"), node("h3", "", release.rocm_version || "Version not reported"), node("span", "tag", release.release_type || "Unclassified"));
    if (release.build_variant) identity.append(node("span", "tag", release.build_variant));
    const metadata = append(node("div", "release-meta"), node("span", "", `Build ${release.build_date || "date unavailable"}`), node("span", "mono", release.therock_commit ? `TheRock ${release.therock_commit.slice(0, 10)}` : "Commit not reported"), link("Source document", release.source));
    const top = append(node("div", "release-top"), append(node("div"), identity, metadata), badge(release.overall_status, `Overall: ${labels[outcome({ status: release.overall_status })]}`));
    const platforms = node("div", "release-grid");
    for (const platform of release.platforms || []) {
      const block = append(node("div", "platform-card"), append(node("div", "platform-heading"), node("h4", "", platform.name || "Platform"), badge(platform.status)));
      append(block, append(node("div", "platform-status-line"), node("span", "", "ROCm artifact build"), badge(platform.build_status, outcome({ status: platform.build_status }) === "success" ? "Build available" : undefined)));
      const pipelines = platform.pipelines || {};
      const entries = Object.entries(pipelines).filter(([name]) => ["rocm", "pytorch", "jax"].includes(name));
      if (entries.length) {
        const table = makeTable(["Pipeline", "Build", "Validation matrix"], entries.map(([name, data]) => [node("strong", "", name === "rocm" ? "ROCm" : name === "pytorch" ? "PyTorch" : "JAX"), badge(data.build?.status), matrixCell(data.test, data.test_full)]));
        block.append(table);
      } else block.append(matrixCell(platform.test_counts));
      if (pipelines.native_packages) {
        const packages = node("div", "matrix-counts");
        for (const [kind, result] of Object.entries(pipelines.native_packages)) append(packages, node("span", "", kind.toUpperCase()), badge(result?.status));
        block.append(packages);
      }
      block.append(node("p", "arch-list", (platform.architectures || []).join(" · ") || "GPU architecture coverage not reported"));
      const artifacts = node("div", "artifact-links");
      for (const [kind, url] of Object.entries(platform.urls || {})) if (url && evidenceURL(url, activeSource)) artifacts.append(link(kind === "artifacts" ? "Artifact index" : kind.toUpperCase(), url));
      if (artifacts.childNodes.length) block.append(artifacts);
      platforms.append(block);
    }
    if (!platforms.childNodes.length) platforms.append(empty("Platform data not reported", "The release may still be initializing.", true));
    append(card, top, platforms);
    if (release.regressions?.length) card.append(node("div", "release-regressions", `New failures versus comparable release: ${release.regressions.map(objectText).join(" · ")}`));
    card.append(node("p", "section-note release-footnote", `Source updated ${timestamp(release.source_updated_at)} · collected ${timestamp(release.observed_at)}`));
    return card;
  });
  replace("release-list", ...cards);
  if (releases.length > visibleReleases) $("release-list").append(moreButton(`Show more releases (${releases.length - visibleReleases} remaining)`, () => { visibleReleases += 8; renderReleases(); }));
}
function matrixCell(counts, full) {
  const container = node("div");
  if (!counts || !Object.values(counts).some((value) => Number(value) > 0)) container.append(node("span", "cell-secondary", "Not reported / not run"));
  else {
    const values = node("div", "matrix-counts");
    for (const [status, label] of [["success", "passed"], ["failure", "failed"], ["in_progress", "running"], ["cancelled", "cancelled"], ["skipped", "skipped"]]) {
      if (Number(counts[status]) > 0) values.append(node("span", status === "failure" ? "count-fail" : "", `${number(counts[status])} ${label}`));
    }
    append(container, values, node("span", "cell-secondary", "Matrix entries, not test cases"));
  }
  if (full) append(container, node("div", "cell-secondary", "Full-suite matrix"), matrixCell(full));
  return container;
}

function setOptions(id, values, placeholder) {
  const select = $(id);
  const previous = select.value;
  select.replaceChildren(new Option(placeholder, ""), ...[...new Set(values.filter(Boolean))].sort().map((value) => new Option(value, value)));
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
}
function renderRuns() {
  const runs = filterRuns(feed.currentRuns, { repo: $("repo-filter").value, state: $("outcome-filter").value, event: $("event-filter").value, search: $("search-filter").value });
  $("run-count").textContent = `${number(runs.length)} of ${number(feed.currentRuns.length)} collected runs`;
  if (!runs.length) { replace("run-list", empty("No matching workflow runs", feed.currentRuns.length ? "Try changing your filters." : "The Quartz collector has not published workflow-run evidence yet.")); return; }
  replace("run-list", ...runs.slice(0, visibleRuns).map(runRow));
  if (runs.length > visibleRuns) $("run-list").append(moreButton(`Show more runs (${runs.length - visibleRuns} remaining)`, () => { visibleRuns += 75; renderRuns(); }));
}
function runRow(run) {
  const row = node("details", "run-row");
  const identity = append(node("div", "run-title"), node("span", "run-chevron", "▸"), append(node("div"), node("div", "run-name", run.name || "Unnamed workflow"), node("div", "run-meta", `${run.repo || "Repository unavailable"} · ${run.branch || "Branch unavailable"} · ${(run.head_sha || "").slice(0, 8)}`)));
  const context = append(node("div", "run-context"), node("strong", "", run.event || "Trigger unavailable"), node("span", "", `Attempt ${run.run_attempt || 1}`));
  const state = append(node("div"), badge(outcome(run)));
  if (run.recovered) state.append(node("div", "cell-secondary", "Recovered on retry"));
  const time = node("div", "run-time", relativeAge(run.created_at));
  time.title = timestamp(run.created_at);
  append(row, append(node("summary"), identity, context, state, time));
  const details = node("div", "run-evidence");
  const jobs = run.jobs || [];
  const attempts = (feed.run_attempts || feed.runs).filter((item) => item.repo === run.repo && (item.id ?? item.run_id) === (run.id ?? run.run_id));
  const attemptLine = [...attempts].sort((a, b) => Number(a.run_attempt || 1) - Number(b.run_attempt || 1)).map((item) => `#${item.run_attempt || 1}: ${labels[outcome(item)]}`).join(" · ");
  append(details, append(node("div", "evidence-top"), node("p", "", `Run ${run.id ?? run.run_id} · ${attemptLine || `Attempt ${run.run_attempt || 1}`}`), link("Open workflow evidence", run.html_url)));
  if (run.recovered) details.append(node("p", "section-note", "A failed earlier attempt and a successful latest attempt were observed. Recovery alone does not prove a flaky test."));
  if (jobs.length) {
    details.append(makeTable(["Job / failed steps", "Outcome", "Run time", "Queue", "Runner"], jobs.map((job) => {
      const jobName = append(node("div"), link(job.name || `Job ${job.id}`, job.html_url));
      const failedSteps = (job.steps || []).filter((step) => outcome(step) === "failure");
      if (failedSteps.length) jobName.append(node("div", "cell-secondary", failedSteps.map((step) => `Failed: ${step.name}`).join(" · ")));
      return [jobName, badge(outcome(job)), duration(job.duration_seconds), duration(job.queue_seconds), append(node("div"), node("span", "", job.runner_name || "Not reported"), node("div", "cell-secondary", (job.labels || []).join(" · ")))];
    })));
  } else details.append(empty("Job details not collected", "Open the workflow evidence for its original results. Missing jobs are not counted as passes.", true));
  append(details, node("p", "section-note", `Started ${timestamp(run.started_at)} · completed ${timestamp(run.completed_at)}${run.release_id ? ` · release ${run.release_id}` : " · release association not established"}`));
  row.append(details);
  return row;
}
function makeTable(headers, rows) {
  const container = node("div", "table-wrap");
  container.tabIndex = 0;
  const table = node("table", "data-table");
  const head = node("thead");
  const tr = node("tr");
  for (const text of headers) { const cell = node("th", "", text); cell.scope = "col"; tr.append(cell); }
  head.append(tr);
  const body = node("tbody");
  for (const columns of rows) {
    const row = node("tr");
    for (const value of columns) { const cell = node("td"); if (value instanceof Node) cell.append(value); else cell.textContent = value ?? "—"; row.append(cell); }
    body.append(row);
  }
  append(table, head, body); container.append(table); return container;
}
function moreButton(text, handler) { const button = node("button", "text-button more-button", text); button.type = "button"; button.addEventListener("click", handler); return button; }

function renderTests() {
  const tests = feed.tests;
  const failures = tests.filter((test) => ["fail", "failure", "error"].includes(test.status));
  $("test-count").textContent = tests.length ? `${number(tests.length)} reported cases` : "JUnit evidence unavailable";
  replace("test-summary");
  if (!tests.length) { replace("test-list", empty("No individual test cases have been collected", "JUnit artifacts must be available and artifact collection enabled, or a test report must be ingested. Release matrix results above remain available.")); return; }
  for (const [label, count] of [["Passed", tests.filter((test) => ["pass", "success"].includes(test.status)).length], ["Failed / error", failures.length], ["Skipped", tests.filter((test) => test.status === "skipped").length]]) $("test-summary").append(append(node("span"), node("strong", "", number(count)), document.createTextNode(label)));
  if (!failures.length) { replace("test-list", empty("No failures in the reported test cases", "This covers only the collected JUnit evidence. It is not a statement that every expected suite ran.")); return; }
  const cases = failures.slice(0, 200).map((test) => {
    const details = node("details", "test-case");
    const text = append(node("div"), node("div", "test-name", `${test.classname ? `${test.classname}.` : ""}${test.name || "Unnamed case"}`), node("div", "test-context", `${test.suite || "Suite unavailable"} · ${test.repo || "Repository unavailable"} · run ${test.run_id || "?"} / attempt ${test.run_attempt || 1}${test.arch ? ` · ${test.arch}` : ""}`));
    append(details, append(node("summary"), text, badge(test.status)), node("pre", "test-message", test.message || "The report marked this case as failed but did not include an assertion message."));
    if (test.source) {
      const source = /^https?:\/\//i.test(test.source) ? link("Open test report source", test.source) : node("span", "mono", `Report source: ${test.source}`);
      details.append(append(node("div", "test-source"), source));
    }
    return details;
  });
  replace("test-list", ...cases);
  if (failures.length > 200) $("test-list").append(node("p", "section-note", `Showing the first 200 of ${number(failures.length)} failures. The Quartz feed contains the full collected set.`));
}

function renderDownstream() {
  if (!feed.downstream.length) { replace("downstream-list", empty("No downstream compatibility results reported", "A green upstream build does not establish downstream compatibility. Submit a result pinned to its Quartz release to populate this view.")); return; }
  replace("downstream-list", makeTable(["Project / source", "ROCm release", "Environment", "Suite", "Outcome", "Observed"], feed.downstream.map((result) => [append(node("div"), link(result.project || result.repo || "Project unavailable", result.url), node("div", "cell-secondary mono", (result.head_sha || "").slice(0, 10))), result.rocm_version || result.release_id || "Not reported", [result.platform, result.arch, result.build_variant].filter(Boolean).join(" · ") || "Not reported", result.suite || "Not reported", badge(result.status), timestamp(result.observed_at)])));
}

function renderCoverage() {
  const collection = feed.collection || {};
  const stats = node("div", "coverage-stats");
  for (const [title, value] of [["Feed generated", timestamp(feed.generated_at)], ["Most recent observation", timestamp(feed.freshness?.latest_observed_at)], ["Collection outcome", collection.status || "Not reported"]]) append(stats, append(node("dl"), node("dt", "", title), node("dd", "", value)));
  const list = node("ul", "coverage-list");
  const gaps = [...feed.gaps];
  if (!gaps.length) gaps.push("No explicit coverage limitations were supplied. This does not imply complete CI coverage.");
  gaps.push("Release validation counters describe matrix entries; JUnit results describe individual cases. These denominators are kept separate.");
  gaps.push("Retry recovery requires an observed failed attempt followed by success. It does not by itself identify flaky tests or their cause.");
  for (const gap of gaps) list.append(node("li", "", objectText(gap)));
  replace("coverage-details", stats, node("h3", "", "Evidence and limitations"), list);
  const coverage = feed.coverage;
  if (coverage && typeof coverage === "object") {
    const structured = node("dl", "coverage-counts");
    for (const [key, value] of Object.entries(coverage)) append(structured, node("dt", "", key.replaceAll("_", " ")), node("dd", "", objectText(value)));
    $("coverage-details").append(structured);
  }
  const errors = Array.isArray(collection.errors) ? collection.errors : collection.errors ? [collection.errors] : [];
  if (errors.length) {
    const box = append(node("div", "coverage-errors"), node("strong", "", "Collection errors / unavailable sources"));
    const items = node("ul");
    for (const error of errors) items.append(node("li", "", objectText(error)));
    box.append(items); $("coverage-details").append(box);
  }
}

function renderAll() {
  renderFreshness(); renderOverview(); renderReleases();
  setOptions("repo-filter", feed.currentRuns.map((run) => run.repo), "All repositories");
  setOptions("event-filter", feed.currentRuns.map((run) => run.event), "All triggers");
  renderRuns(); renderTests(); renderDownstream(); renderCoverage();
}
function renderInitialEmpty() {
  replace("metrics", metric("ROCm builds available", "—", "Waiting for Quartz release evidence"), metric("Failed workflow runs", "—", "Waiting for collected workflow results"), metric("Work in progress", "—", "Source not loaded"), metric("Recovered on retry", "—", "Attempt history not loaded"), metric("Failing test cases", "—", "Test reports not loaded"));
  for (const id of ["trend-chart", "attention", "release-list", "run-list", "test-list", "downstream-list", "coverage-details"]) replace(id, empty("Waiting for the Quartz health feed", "Configure the data source above, then refresh. No example results are substituted."));
}

async function loadFeed() {
  if (activeRequest) activeRequest.abort();
  const controller = new AbortController();
  activeRequest = controller;
  const timeout = setTimeout(() => controller.abort(), 30000);
  $("refresh").disabled = true;
  $("refresh").textContent = "Refreshing…";
  showFeedState("loading", "Reading the Quartz health feed", feed ? "The previously loaded results remain visible until the refresh completes." : "Loading collected evidence. No browser-side GitHub API calls are made.");
  try {
    const response = await fetch(activeSource, { signal: controller.signal, cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer", headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`The feed returned HTTP ${response.status}. Publish health.json on the data branch, or select another Quartz feed.`);
    if (Number(response.headers.get("content-length")) > 20_000_000) throw new Error("The feed exceeds the dashboard's 20 MB limit. Publish a bounded dashboard export.");
    const text = await response.text();
    if (text.length > 20_000_000) throw new Error("The feed exceeds the dashboard's 20 MB limit. Publish a bounded dashboard export.");
    const next = normalizeFeed(JSON.parse(text));
    if (activeRequest !== controller) return;
    feed = next;
    $("last-loaded").textContent = `Loaded ${new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
    renderAll();
  } catch (error) {
    if (activeRequest !== controller) return;
    const reason = error.name === "AbortError" ? "The request timed out after 30 seconds." : error.message;
    showFeedState("error", feed ? "Refresh failed — showing the previous collection" : "Quartz health feed is unavailable", `${reason}${feed ? ` Last feed generated ${timestamp(feed.generated_at)}.` : " No CI health conclusion can be drawn until data loads."}`);
    $("source-status").textContent = "Feed unavailable";
    $("last-loaded").textContent = feed ? "Refresh failed" : "No data loaded";
  } finally {
    clearTimeout(timeout);
    if (activeRequest === controller) { activeRequest = null; $("refresh").disabled = false; $("refresh").textContent = "Refresh data"; }
  }
}

function chooseSource(value, persist = true) {
  activeSource = sourceURL(value, document.baseURI);
  $("source-url").value = activeSource;
  $("raw-feed").href = activeSource;
  if (persist) { try { localStorage.setItem("quartz-health-source", activeSource); } catch { /* Storage can be disabled. */ } }
}
$("source-form").addEventListener("submit", (event) => {
  event.preventDefault();
  try { chooseSource($("source-url").value); feed = null; renderInitialEmpty(); loadFeed(); }
  catch (error) { showFeedState("error", "Invalid data source", error.message); }
});
$("reset-source").addEventListener("click", () => { chooseSource(DEFAULT_SOURCE); feed = null; renderInitialEmpty(); loadFeed(); });
$("refresh").addEventListener("click", loadFeed);
for (const id of ["repo-filter", "outcome-filter", "event-filter", "search-filter"]) $(id).addEventListener(id === "search-filter" ? "input" : "change", () => { visibleRuns = 75; if (feed) renderRuns(); });
renderInitialEmpty();
try {
  let saved;
  try { saved = localStorage.getItem("quartz-health-source"); } catch { /* Optional preference. */ }
  chooseSource(new URL(location.href).searchParams.get("source") || saved || DEFAULT_SOURCE, false);
  loadFeed();
} catch (error) { showFeedState("error", "Invalid data source", error.message); }
setInterval(() => { if (feed && !activeRequest) renderFreshness(); }, 60000);
setInterval(() => { if (feed && !activeRequest && document.visibilityState === "visible") loadFeed(); }, 300000);
