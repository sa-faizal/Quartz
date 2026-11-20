# ROCm CI Health dashboard

This buildless dashboard reads one Quartz `health.json` export. The browser does
not call GitHub's API, download CI artifacts or require a token. The Quartz
collector produces the release, workflow, job, test and downstream evidence.

Serve this directory with any static HTTP server. No Node dependencies or build
step are required. For example, from the repository root:

```console
python -m http.server 8000 --directory dashboard
```

The default feed is
`https://raw.githubusercontent.com/sa-faizal/Quartz/ci-health-data/health.json`.
Change it in **Data source and collection details**, or open
`/?source=https%3A%2F%2Fexample.com%2Fhealth.json`. The explicit query parameter
takes precedence over the saved browser preference. Reset restores the default.
HTTPS is required except for HTTP localhost previews. A remotely hosted feed must
allow cross-origin reads; raw GitHub already does. Do not put tokens in feed URLs.

The health feed uses `schema_version: "1.0"`. See the Quartz CI health
documentation for collection and publication. Supported top-level arrays are
`releases`, `runs`, `tests`, `downstream` and `gaps`. `freshness`, `collection`,
`coverage`, `generated_at` and `run_attempts` provide provenance and limitations.
Missing arrays render as unavailable evidence. Unsupported schema majors and
malformed array fields produce explicit errors. A failed refresh retains the
previous collection with a failure banner; it does not substitute example data.
An open, visible dashboard refreshes every five minutes. Source freshness is
re-evaluated every minute even between refreshes.

Release test counters are labeled as matrix entries. Individual cases appear
only from JUnit evidence. Latest workflow attempts are counted once; retry
recovery requires an observed earlier failed attempt, and does not prove a flaky
test. The outcome chart groups the latest attempt by UTC creation date and
explicitly calls out missing dates. Source timestamps and collection gaps remain
visible even when jobs are green.

Run the dependency-free model tests with Node.js 20 or newer:

```console
node --test dashboard/health-model.test.mjs
```
