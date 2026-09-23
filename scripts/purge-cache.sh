#!/bin/sh
set -eu
# workers.dev deployments use content-addressed static assets and v0.12 critical APIs are no-store.
# If a custom Cloudflare zone is added later, set CF_ZONE_ID and CLOUDFLARE_API_TOKEN to purge it here.
if [ -z "${CF_ZONE_ID:-}" ] || [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
  printf '%s\n' 'No custom-zone cache configured; workers.dev cache generation will roll with the deployment.'
  exit 0
fi
curl -fsS -X POST "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/purge_cache" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"purge_everything":true}' >/dev/null
printf '%s\n' 'Custom-zone cache purged.'
