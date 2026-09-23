#!/bin/sh
set -eu
WORKER_NAME="${INGEST_WORKER_NAME:-hidden-conviction-ingest}"

secret_names() {
  npx wrangler secret list --name "$WORKER_NAME" 2>/dev/null | grep '"name"' | sed -E 's/.*"name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/' || true
}

read_dev_var() {
  key="$1"
  [ -f .dev.vars ] || return 1
  value="$(grep -E "^${key}=" .dev.vars | tail -1 | cut -d= -f2- || true)"
  value="${value#\"}"; value="${value%\"}"
  [ -n "$value" ] || return 1
  printf '%s' "$value"
}

NAMES="$(secret_names)"
if ! printf '%s\n' "$NAMES" | grep -qx 'ADMIN_SECRET'; then
  value="${ADMIN_SECRET:-}"
  if [ -z "$value" ]; then value="$(read_dev_var ADMIN_SECRET || true)"; fi
  if [ -z "$value" ]; then
    value="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
    umask 077
    printf '%s\n' "$value" > .admin-secret-v013
    printf '%s\n' 'Generated a new ADMIN_SECRET and stored it in .admin-secret-v013 (local only).'
  fi
  printf '%s' "$value" | npx wrangler secret put ADMIN_SECRET --name "$WORKER_NAME" >/dev/null
  printf '%s\n' 'Configured ADMIN_SECRET for hidden-conviction-ingest.'
fi

NAMES="$(secret_names)"
if ! printf '%s\n' "$NAMES" | grep -qx 'SEC_USER_AGENT'; then
  value="${SEC_USER_AGENT:-}"
  if [ -z "$value" ]; then value="$(read_dev_var SEC_USER_AGENT || true)"; fi
  if [ -z "$value" ]; then
    printf '%s\n' 'SEC_USER_AGENT is required once for the new ingestion Worker.'
    printf '%s' 'Enter a descriptive SEC user agent including a contact email: '
    IFS= read -r value
  fi
  if [ -z "$value" ]; then
    printf '%s\n' 'SEC_USER_AGENT cannot be empty.' >&2
    exit 1
  fi
  printf '%s' "$value" | npx wrangler secret put SEC_USER_AGENT --name "$WORKER_NAME" >/dev/null
  printf '%s\n' 'Configured SEC_USER_AGENT for hidden-conviction-ingest.'
fi

printf '%s\n' 'Ingestion Worker secrets are configured.'
