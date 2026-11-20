# CI health feed

This fork adds an operational CI evidence feed alongside the upstream release
`status.json` contract. It does not change the upstream status schema or claim
that upstream Quartz's planned services are deployed.

The dashboard fetches only `health.json` from Quartz. Quartz collects public
TheRock and rockrel Actions metadata, release status snapshots and explicitly
attributed JUnit evidence. It accepts maintainer-authorized downstream reports.
There is no GitHub credential or direct GitHub API call in the dashboard.

## Run locally

Python 3.12 and the standard library suffice for the new health collector.
Authenticate `gh` or set `GH_TOKEN`/`GITHUB_TOKEN` in the environment. The token
needs read access to Actions metadata and artifacts for each configured source.

```bash
python scripts/ci_health/cli.py --db health-state/health.sqlite collect \
  --config config/ci-health.json --output health-state/health.json
python scripts/ci_health/cli.py --db health-state/health.sqlite export \
  --output health-state/health.json
```

Configure monitored repositories and release streams in
[`config/ci-health.json`](../../config/ci-health.json). A polling collection
updates an existing release even when its version has not changed: late results
and retries are evidence. Changed raw snapshots and attempt history remain in
SQLite. Exit 2 means partial coverage: the feed is still written and explains
the gaps. Exit 1 means the command could not safely complete.

`history_days` imports the previous seven nightly release documents and the
latest-good pointer to seed comparable release history. It does not fan out
into every historical workflow. Missing historical documents remain explicit
coverage warnings. Workflow metadata is separately bounded by `days`,
`max_runs`, `max_correlated_runs`, `max_attempts` and `max_requests`.

## Published feed and scheduling

[`Publish CI health`](../../.github/workflows/ci_health.yml) runs every 30
minutes on the fork's default branch and can also be dispatched manually. It
reads and updates `health.sqlite` and `health.json` on `ci-health-data`.
The default branch must contain this workflow; scheduled Actions may be delayed.
Collection and downstream imports share a concurrency group to prevent lost
updates. Bootstrap `ci-health-data` with a local collection before enabling it.
The workflow runs automatically only in `sa-faizal/Quartz`; another deployment
must explicitly set repository variable `CI_HEALTH_ENABLED=true`.

The consumer endpoint is:

```text
https://raw.githubusercontent.com/sa-faizal/Quartz/ci-health-data/health.json
```

Only configure public evidence in this public fork. Both the exported feed and
its SQLite history are public. No email, credentials, full logs, environment
variables or private-repository content belong in the feed.

## Test case evidence

Workflow test-matrix counters are different from individual test cases. JUnit
is an additional source; a passing matrix entry does not synthesize test cases.
The parser handles nested suites and failure/error/skipped outcomes. It rejects
DTD/entity declarations, oversized XML and unsafe or excessive ZIP members.

Enable `include_artifacts` in the collector config to inspect artifact evidence.
For reliable attribution, producers should upload JUnit XML using an explicit
attempt in the artifact name:

```yaml
- name: Upload Quartz test evidence
  if: always()
  uses: actions/upload-artifact@v4
  with:
    name: quartz-junit-attempt-${{ github.run_attempt }}
    path: test-results/**/*.xml
    if-no-files-found: warn
```

Matrix producers should add a unique suffix to the name for each upload.
An artifact without trustworthy attempt attribution is reported as a gap; it
is never silently assigned to an earlier attempt. Expired artifacts cannot
be recovered by the collector. To import a known local report:

```bash
python scripts/ci_health/cli.py --db health-state/health.sqlite ingest-junit \
  --report results.xml --repo OWNER/PROJECT --run-id 123 --run-attempt 2 \
  --job-id 456 --arch gfx942 --output health-state/health.json
```

## Downstream reporting

The workflow-dispatch importer requires repository write access. It accepts
only a constrained observation linked to a known Quartz release and a concrete
GitHub workflow run. A downstream repository's ordinary `GITHUB_TOKEN` cannot
write to Quartz: use a GitHub App installation token or a fine-grained token
with Actions write access to the Quartz fork. Do not grant untrusted PR jobs
that credential. Manual observations are labeled `maintainer_import`; they are
not presented as independently verified GitHub results.

Report shape (copy a real `release_id` from the feed):

```json
{
  "schema_version": "1.0",
  "release_id": "COPY_FROM_HEALTH_FEED",
  "project": "debug-tools",
  "repo": "OWNER/PROJECT",
  "head_sha": "FULL_40_CHARACTER_COMMIT_SHA",
  "run_id": 123,
  "run_attempt": 1,
  "platform": "linux",
  "arch": "gfx942",
  "suite": "smoke",
  "status": "success",
  "observed_at": "2026-11-19T00:00:00Z",
  "build_variant": "release"
}
```

```bash
python scripts/ci_health/report_downstream.py --report report.json
python scripts/ci_health/report_downstream.py --report report.json --send
```

The first command validates without sending. The second dispatches
[`Import downstream CI health`](../../.github/workflows/ingest_ci_health.yml).
Dispatch success means accepted for processing, not published: inspect that
workflow's result. Local ingestion is also supported:

```bash
python scripts/ci_health/cli.py --db health-state/health.sqlite ingest-downstream \
  --report report.json --output health-state/health.json
```

## Meaning and limits

- Build availability and validation health are separate outcomes. Global red
  does not mean every artifact is unavailable.
- Successful retries after an observed failure are **recovery**, not proof of
  a flaky test. Unobserved attempts cannot establish first-attempt success.
- Missing, stale, unreported, skipped and cancelled results do not become passes.
- Workflow collection includes PR, post-merge, scheduled, manual and sanitizer
  workflows within the configured window and bounds; it does not imply complete
  coverage of all ROCm repositories or historical runs.
- A release's requested architectures are not its expected test denominator.
  Unsupported or unreported matrix cells are not silently counted as passing.
- Test cases cover only successfully ingested reports. Coverage percentages,
  performance regressions, ownership and root causes require additional evidence.
- Queue and duration measurements are shown only when timestamps support them.
- Limits, source errors and incomplete job/artifact reads remain visible in
  `collection` and `gaps`. Browser refresh does not make old evidence fresh.
- SQLite snapshots preserve collected history, not history from before collection.
  Over time its size grows; archive/rotate state according to retention needs.
- The browser feed limits each detail list to `export_limit` (default 200).
  Attempt history in the feed contains metadata; full historical job details
  remain in SQLite. Truncation and total counts are explicit, and failing test
  evidence takes precedence over passing cases. The JSON is compact to reduce
  browser transfer size.

Run the full existing and new test suites with `python -m pytest scripts docs`.
