# Hidden Conviction v0.14.0

v0.14.0 is the adaptive-ingestion and operational-integrity release.

## What changed

### Self-regulating ingestion

The filing pipeline now selects one of five runtime modes based on backlog size, SEC failures, and SEC latency. Effective filing concurrency can scale from one to five consumers without redeploying. A D1 lease table enforces the selected concurrency even though Cloudflare Queues is configured with a fixed upper bound of five.

When the filing backlog is high, the controller prioritizes filing processing over issuer-identity refresh work. When the backlog returns to normal, background identity repair increases again.

SEC requests now have a per-worker minimum request cycle so the combined ingestion, cron, and 13F workload retains headroom under SEC fair-access guidance.

### Backlog visibility

System Console now exposes:

- filings discovered per hour
- filings processed per hour
- net backlog change per hour
- estimated clearance time
- controller mode and target concurrency
- a persisted 24-hour backlog chart
- active operational alerts

### Recovery

Transient filing failures such as timeouts, rate limits, and server errors are automatically requeued, with a two-replay cap and replay receipts. Permanent/parser failures remain operator-controlled. The operator console can also reprocess all current filing failures in one action.

### Data and telemetry integrity

Migration 0033 normalizes legacy compact SEC filing dates, adds backlog/controller/alert/performance history, adds adaptive ingestion leases, and records automated replay state. Cron health reconciles the durable cron table with the existing runtime success receipt.

Issuer identity health now includes stale-row count, active refresh batch size, and estimated repair time.

### Deployment observability

Deployment TTFB receipts are persisted. System Console shows deployment history and an explicit latest-versus-prior comparison for Home, Health, System, and queue backlog.

### UI correction

The Home KPIs no longer visually concatenate the metric labels and values. `Highest current HCS` and `Published signals` use separate grid columns, while `Threshold HCS ≥ 80` is rendered on its own line. This fixes the appearance where `43` and the final `0` of the threshold/count appeared unnaturally bold and attached to the preceding text.

## Validation

- 230 Python tests pass
- 18 frontend JavaScript tests pass
- measured Python coverage: about 35%
- enforced coverage floor: 34%
- adaptive controller module coverage: 86%
- Python compilation passes
- JavaScript syntax passes
- shell syntax passes
- JSON/JSONC configuration validation passes
- bundle budgets pass

Ruff, BasedPyright and ESLint remain part of the normal Mac/GitHub deployment validation. They could not be executed in the isolated build sandbox because registry packages are unavailable there.
