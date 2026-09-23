#!/bin/sh
set -eu

DB_NAME="${DB_NAME:-hidden-conviction-db}"
BASE_URL="${BASE_URL:-https://hidden-conviction.aotwone.workers.dev}"
INGEST_URL="${INGEST_URL:-https://hidden-conviction-ingest.aotwone.workers.dev}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${BACKUP_DIR:-backups}"
BACKUP_FILE="$BACKUP_DIR/${DB_NAME}-pre-v0.14.0-$STAMP.sql"

PUBLIC_WRANGLER_BACKUP=""

restore_public_wrangler() {
  if [ -n "$PUBLIC_WRANGLER_BACKUP" ] && [ -f "$PUBLIC_WRANGLER_BACKUP" ]; then
    cp "$PUBLIC_WRANGLER_BACKUP" wrangler.jsonc
    rm -f "$PUBLIC_WRANGLER_BACKUP"
    PUBLIC_WRANGLER_BACKUP=""
  fi
}

trap 'restore_public_wrangler' EXIT HUP INT TERM

deploy_python_worker() {
  target_config="$1"
  PUBLIC_WRANGLER_BACKUP="$(mktemp)"
  cp wrangler.jsonc "$PUBLIC_WRANGLER_BACKUP"
  cp "$target_config" wrangler.jsonc
  uv run pywrangler deploy
  restore_public_wrangler
}

printf '%s\n' '==> Stamping build metadata'
python3 ./scripts/stamp-build.py

printf '%s\n' '==> Running local validation'
./scripts/smoke.sh

printf '%s\n' '==> Checking bundle budgets'
./scripts/bundle-budget.sh

printf '%s\n' '==> Creating remote D1 backup'
mkdir -p "$BACKUP_DIR"
npx wrangler d1 export "$DB_NAME" --remote --output="$BACKUP_FILE"
printf 'Backup: %s\n' "$BACKUP_FILE"

printf '%s\n' '==> Applying remote D1 migrations'
npx wrangler d1 migrations apply "$DB_NAME" --remote

printf '%s\n' '==> Deploying Python ingestion/scoring Worker'
deploy_python_worker wrangler.ingest.jsonc

printf '%s\n' '==> Ensuring ingestion Worker secrets'
./scripts/ensure-ingest-secrets.sh

printf '%s\n' '==> Verifying ingestion backend before public cutover'
INGEST_HEALTH="$(curl -fsS "$INGEST_URL/api/health")"
python3 - "$INGEST_HEALTH" <<'PY'
import json, sys
p=json.loads(sys.argv[1])
assert p.get('version') == '0.14.0', p
assert p.get('checks',{}).get('database') == 'ok', p
assert p.get('checks',{}).get('sec_user_agent') == 'configured', p
print('Ingestion backend healthy for v0.14.0')
PY

printf '%s\n' '==> Deploying isolated 13F Worker'
deploy_python_worker wrangler.13f.jsonc

printf '%s\n' '==> Deploying lightweight public edge Worker'
npx wrangler deploy --config wrangler.jsonc

printf '%s\n' '==> Purging optional custom-zone cache'
./scripts/purge-cache.sh || true

printf '%s\n' '==> Verifying production'
./scripts/verify-production.sh "$BASE_URL"

printf '%s\n' '==> Enforcing production performance budgets'
./scripts/perf-budget.sh "$BASE_URL"

printf '%s\n' '==> Recording deployment performance history'
if [ -f .deploy-metrics.json ]; then
  ADMIN_KEY="${ADMIN_SECRET:-}"
  if [ -z "$ADMIN_KEY" ] && [ -f .admin-secret-v013 ]; then ADMIN_KEY="$(cat .admin-secret-v013)"; fi
  if [ -n "$ADMIN_KEY" ]; then
    BUILD_ID="$(curl -fsS "$BASE_URL/api/build" | python3 -c 'import json,sys; print(json.load(sys.stdin)["build_id"])')"
    METRICS="$(python3 - "$BUILD_ID" .deploy-metrics.json <<'PY'
import json,sys
p=json.load(open(sys.argv[2])); p['build_id']=sys.argv[1]; print(json.dumps(p,separators=(',',':')))
PY
)"
    curl -fsS -X POST "$BASE_URL/api/admin/deployment-metrics" -H 'Content-Type: application/json' -H "X-Admin-Key: $ADMIN_KEY" --data "$METRICS" >/dev/null
    printf '%s\n' 'Deployment performance receipt stored.'
  else
    printf '%s\n' 'Local admin key unavailable; skipped deployment performance receipt.'
  fi
fi

printf '%s\n' '==> Hidden Conviction v0.14.0 deployment verified'
