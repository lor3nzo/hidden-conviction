# Hidden Conviction v0.13.0

Generated from the repository state and changelog.

## Changelog excerpt

## 0.13.0 - 2026-09-22

* Split the public site from Python ingestion/scoring into a lightweight JavaScript edge Worker, a Python ingestion Worker, and the existing isolated 13F Worker.
* Add a Cloudflare service binding from the public Worker to the ingestion/API Worker and deploy backend-first with public cutover last.
* Replace the monolithic hidden-view HTML shell with distinct Home, Company, System, and Methodology documents.
* Render route-specific initial HTML and metadata without shipping unrelated page markup.
* Add the requested centered informational/educational disclaimer to every public page.
* Render Home directly from D1 at the edge and record home-query timing metrics.
* Add query-performance storage, targeted D1 indexes, fixed admin EXPLAIN QUERY PLAN diagnostics, and query-plan regression coverage.
* Expand System Console with pipeline flow, cron history, error history, seven-day pipeline activity/latency, query timing, and retained admin replay controls.
* Move operational telemetry helpers from the Python entrypoint into `src/telemetry.py`; move public SSR to `src/public_worker.js`.
* Add migration 0032 with read-path indexes and `query_metrics`.
* Add transactional migration rollback and indexed-query-plan tests.
* Pin project tooling versions, add release-note generation, and exclude generated admin-secret/coverage artifacts from Git.
* Add raw-origin Playwright route isolation checks, build-header checks, accessibility coverage, and v0.13 screenshot artifacts.
* Establish a measured 34% Python coverage baseline with a 30% enforced CI floor.
* Current lightweight validation: 209 Python tests and 16 frontend JavaScript tests pass.

