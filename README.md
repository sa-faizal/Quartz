# Quartz CI health data

`health.json` is the dashboard's only consumer feed. `health.sqlite.gz`
preserves the collected release snapshots, run attempts, jobs and reports.
The collector restores SQLite locally, updates it and publishes a deterministic
compressed archive. Missing reports and collection limits remain explicit.

Collector source and operating instructions are on the
[`develop` branch](https://github.com/sa-faizal/Quartz/tree/develop/docs/ci-health).
