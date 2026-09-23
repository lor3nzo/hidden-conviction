# Hidden Conviction v0.14.0

Hidden Conviction is a filing-driven SEC research signal engine running on Cloudflare Workers, D1, Queues, and Cron.

v0.14.0 focuses on adaptive ingestion, backlog elimination, data integrity, and operational observability. The HCS scoring model is unchanged.

## Architecture

```text
Browser
  |
  v
hidden-conviction                 JavaScript public edge Worker
  |  direct D1 reads for Home
  |  Cloudflare Service Binding
  v
hidden-conviction-ingest          Python/FastAPI ingestion + scoring + APIs
  |  adaptive controller + queue consumer
  |  cron + SEC discovery + score recompute
  |
  +------------------------------> hidden-conviction-13f

Shared D1: hidden-conviction-db
```

## v0.14 highlights

- Adaptive filing ingestion with `protect`, `cautious`, `steady`, `accelerated`, and `catch_up` modes.
- Effective filing concurrency scales from 1 to 5 using D1 cooperative leases under a fixed Cloudflare Queue ceiling.
- SEC request pacing preserves rate-limit headroom while allowing backlog catch-up.
- Hourly discovery/processing throughput, backlog velocity, clearance ETA, and a persisted 24-hour backlog trend.
- Bounded automatic replay for transient filing failures plus operator replay-all.
- Operational state vocabulary: Operational, Delayed, Degraded, Failed.
- Cron receipt reconciliation and active alert thresholds.
- Stale issuer-identity repair telemetry and adaptive refresh batching.
- Deployment performance history and latest-versus-prior comparison.
- Legacy compact filing dates normalized by migration 0033 and defensively normalized in the public layer.
- `X-Hidden-Conviction-Build` remains on public and API responses.
- Home KPI layout fixes the `HCS43` / `≥800` visual collision by separating labels, values, and threshold text.
- `src/ingestion_control.py` and `src/system_health.py` reduce entrypoint concentration.

## Disclaimer

The centered informational and educational disclaimer remains on Home, Company, System, and Methodology pages.

## Validation

```text
230 Python tests passed
18 frontend JavaScript tests passed
~35% measured Python coverage
34% enforced coverage floor
86% coverage on the new ingestion controller
Python compile passed
JavaScript syntax passed
Shell syntax passed
JSON/JSONC validation passed
Bundle budgets passed
```

Ruff, BasedPyright, ESLint, Playwright, and Axe remain configured for the normal Mac/GitHub workflow.

## Deployment

```bash
./scripts/deploy.sh
```

The deployment workflow:

1. stamps immutable build metadata
2. runs tests, coverage, type/lint checks and frontend checks
3. checks bundle budgets
4. exports a timestamped D1 backup
5. applies migration 0033
6. deploys `hidden-conviction-ingest`
7. validates ingestion secrets and backend health
8. deploys `hidden-conviction-13f`
9. deploys the lightweight public Worker last
10. verifies version, schema, build headers, normalized freshness, adaptive telemetry and route SSR
11. enforces TTFB budgets
12. stores a deployment performance receipt for version-over-version comparison

## Worker configuration

- `wrangler.jsonc`: public JS edge Worker
- `wrangler.ingest.jsonc`: Python ingestion/scoring Worker
- `wrangler.13f.jsonc`: isolated Python 13F Worker

## Key public API

- `GET /api/version`
- `GET /api/build`
- `GET /api/health`
- `GET /api/system`
- `GET /api/freshness`
- `GET /api/search?q={query}`
- `GET /api/signals`
- `GET /api/candidates`
- `GET /api/company/{ticker-or-cik}`

## Key admin API

Requires `X-Admin-Key`.

- `GET /api/admin/diagnostics`
- `POST /api/admin/replay/filing/{queue_id}`
- `POST /api/admin/replay/failures`
- `POST /api/admin/replay/score/{cik}`
- `POST /api/admin/deployment-metrics`
- `GET /api/admin/query-plans`

See `V0.14.0-SCOPE.md` and `RELEASE-NOTES-v0.14.0.md` for the complete implementation checklist.
