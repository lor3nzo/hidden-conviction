#!/bin/sh
set -eu
BASE_URL="${1:-https://hidden-conviction.aotwone.workers.dev}"
MAX_API_TTFB="${MAX_API_TTFB:-5.0}"
MAX_HOME_TTFB="${MAX_HOME_TTFB:-1.25}"
MAX_PAGE_TTFB="${MAX_PAGE_TTFB:-6.0}"
METRICS_FILE="${METRICS_FILE:-.deploy-metrics.json}"

measure() { curl -fsS -H 'Cache-Control: no-cache' -o /dev/null -w '%{time_starttransfer}' "$1"; }
API_TTFB="$(measure "$BASE_URL/api/health?perf=$(date +%s)")"
HOME_TTFB="$(measure "$BASE_URL/?perf=$(date +%s)")"
SYSTEM_TTFB="$(measure "$BASE_URL/system?perf=$(date +%s)")"
python3 - "$API_TTFB" "$HOME_TTFB" "$SYSTEM_TTFB" "$MAX_API_TTFB" "$MAX_HOME_TTFB" "$MAX_PAGE_TTFB" "$METRICS_FILE" <<'PY'
import json, sys
api, home, system, max_api, max_home, max_page = map(float, sys.argv[1:7])
path=sys.argv[7]
assert api <= max_api, f"/api/health TTFB {api:.3f}s exceeds {max_api:.3f}s budget"
assert home <= max_home, f"/ TTFB {home:.3f}s exceeds {max_home:.3f}s budget"
assert system <= max_page, f"/system TTFB {system:.3f}s exceeds {max_page:.3f}s budget"
with open(path,'w') as f:
    json.dump({"health_ttfb_ms":round(api*1000,1),"home_ttfb_ms":round(home*1000,1),"system_ttfb_ms":round(system*1000,1)},f)
print(f"Performance budgets passed: health={api:.3f}s home={home:.3f}s system={system:.3f}s")
PY
