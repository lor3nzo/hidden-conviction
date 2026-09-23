# Changelog

## 0.14.0 - 2026-09-22

- Added adaptive ingestion modes that scale effective filing concurrency from 1 to 5 based on backlog and SEC health.
- Added conservative SEC request pacing and prioritization of filing drain over identity refresh during backlog conditions.
- Added migration 0033 for normalized filing dates, backlog samples, controller state, ingestion leases, system alerts, deployment metrics, and automated replay receipts.
- Added hourly throughput, net backlog, clearance ETA, 24-hour backlog chart, active alerts, stale-identity repair estimates, and deployment comparison to System Console.
- Added bounded automatic replay for transient filing failures plus one-click operator replay of filing failures.
- Added Operational / Delayed / Degraded / Failed semantics and reconciled cron success receipts.
- Extracted adaptive ingestion control and system-health aggregation from the main Worker entrypoint.
- Raised measured Python coverage to about 35% and the enforced floor from 30% to 34%.
- Fixed Home KPI typography so HCS and published-signal values no longer visually concatenate with their labels or the HCS >= 80 threshold.
- HCS scoring logic remains unchanged.

## 0.13.1 - 2026-09-22

- Fixed SSR home freshness and compact-date formatting.
- Replaced generic degraded health text with queue/error/cron reasons and relative update age.
- Added home HCS KPIs, delayed-data badge, favicon, breadcrumbs, HCS help text, SEC-link copy actions, System refresh, build identity, and robots/sitemap checks.
- Standardized human-facing dates/timestamps and large-number formatting.
- No scoring-model or schema changes.


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

## 0.12.0 - 2026-09-22

* Add immutable deployment build identity with Git SHA, deployment time, build ID, cache generation, response headers, footer rendering, and `/api/build`.
* Eliminate cross-release stale receipts by marking health/version/build/System/freshness APIs `no-store` and verifying version/schema consistency after deploy.
* Add explicit HEAD support for version/build APIs and repair the prior verification-script HEAD failure.
* Add true route-specific SSR for company research and System Console pages, including score/history/evidence and operational stage content.
* Add per-route title, description, canonical URL, and OpenGraph metadata.
* Add SSR-specific ETags and remove inherited static-asset content-length/ETag headers after HTML mutation.
* Add dedicated SEC Access telemetry with request count, errors, and request latency.
* Add queue oldest-pending age and score-recompute latency telemetry.
* Expand cron receipts with discovery/failure/recompute/queue-at-finish counters and stale-running detection.
* Clear stale daily-index recovery errors after successful reconciliation and surface unresolved recovery failures in System health.
* Add expandable public System Console drill-downs.
* Add Playwright deployed-page regression tests, Axe accessibility checks, and CI screenshot artifacts.
* Add production TTFB performance budgets and optional custom-zone cache purge support.
* Add migration 0031 and v0.12 regression/integration tests.
* Current lightweight validation: 195 Python tests and 13 frontend JavaScript tests pass.

## 0.11.0 - 2026-09-22

* Redesign SEC daily-index recovery to use published quarterly directory metadata rather than guessed same-day URLs.
* Add sequential multi-day and quarter-boundary reconciliation catch-up.
* Advance reconciliation watermarks only when discovered filings balance to processed, pending, and failed states.
* Add explicit filing amendment metadata and predecessor linkage.
* Add exponential queue retry scheduling, retry timestamps, dead-letter timestamps, and manual replay endpoints.
* Persist cron run IDs, start/finish timestamps, duration, result summaries, and errors.
* Add deterministic score evidence fingerprints, reason codes, score deltas, and exact evidence replay.
* Add public `ok` / `degraded` / `error` health semantics and a stage-based `/api/system` System Console.
* Add authenticated operator diagnostics with cron, reconciliation, replay, queue, and data-quality receipts.
* Add request IDs, `Server-Timing`, module-import timing, stable ETags, and conditional GET support.
* Add shareable URL filters and browser Back/Forward state restoration.
* Add keyboard/ARIA autocomplete support and SVG HCS history charts.
* Add explicit “Why did the score change?” company research context.
* Server-render initial signal and candidate content for useful crawler/no-JavaScript output.
* Collapse pipeline diagnostics by default and retain one canonical footer version.
* Add Git bootstrap/release-tag tooling and GitHub Actions CI.
* Add Ruff, BasedPyright, ESLint, seeded D1 integration fixtures, and deployment verification coverage.
* Add migration 0030 and v0.11 regression/integration tests.
* Current local validation: 185 Python tests and 9 frontend JavaScript tests pass.

## 0.10.0 - 2026-09-22

* Add a single runtime application-version source and public `/api/version` endpoint.
* Render the deployed version in the site footer and version headers on responses.
* Add exact footer refresh timestamp and public system-status indicator.
* Add signal-family, SIC/industry, minimum-HCS filtering, sorting, and pagination metadata.
* Add signal-family badges, latest evidence date, one-day change, Why Now, and evidence timeline UI.
* Add loading skeletons, retry controls, improved empty states, and mobile filter handling.
* Remove raw component score values from public leaderboard/candidate payloads.
* Add standardized API error envelopes, API schema metadata, request timestamps, and stronger query validation.
* Add SEC filing acceptance timestamps for newly discovered Atom filings.
* Add company SIC/industry enrichment and periodic identity revalidation.
* Add durable discovery watermarks and saturated-feed detection.
* Add automatic same-day SEC daily-master-index recovery when Current Filings feeds approach their 100-row cap.
* Preserve accession-number deduplication while enriching existing filing identity metadata.
* Add pipeline metrics for discovery, processing, retries, failures, and latency.
* Add Cron/runtime status and periodic ticker-to-CIK consistency audits.
* Add consolidated `/api/admin/diagnostics` endpoint.
* Add frontend `HEAD` route support.
* Add one-command smoke, deployment, and production-verification workflows.
* Add migration 0029 and 15 v0.10.0 Python regression tests.
* Add 4 frontend JavaScript tests.
* Keep HCS scoring engine version unchanged at `hcs-v0.8.1`.

## 0.9.0 - 2026-09-22

* Enforce current-day materialized scores on public rankings.
* Add daily rolling-window score refresh.
* Fix CIK-keyed score-recompute requeue semantics.
* Prevent routine aging jobs from endlessly reviving terminal errors.
* Resolve public evidence through exact persisted scoring source IDs.
* Remove internal component provenance from public company responses.
* Add 90-day company score history and 1d/7d/30d change metadata.
* Add company search endpoint and UI.
* Add per-pipeline discovery/freshness telemetry.
* Make filing-family discovery independently fault tolerant.
* Add consolidated admin system-health endpoint.
* Require a real SEC User-Agent contact identity.
* Add browser security headers and API cache policy.
* Add dynamic sitemap and robots directives.
* Add migration 0028 and v0.9.0 regression tests.

- Deployment workflow exports a timestamped remote D1 backup before migrations.
