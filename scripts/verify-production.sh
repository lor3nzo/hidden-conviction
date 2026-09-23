#!/bin/sh
set -eu
BASE_URL="${1:-https://hidden-conviction.aotwone.workers.dev}"
EXPECTED_VERSION="${EXPECTED_VERSION:-0.14.0}"

printf 'Checking %s\n' "$BASE_URL"
health="$(curl -fsS "$BASE_URL/api/health")"
version="$(curl -fsS "$BASE_URL/api/version")"
build="$(curl -fsS "$BASE_URL/api/build")"
system="$(curl -fsS "$BASE_URL/api/system")"
freshness="$(curl -fsS "$BASE_URL/api/freshness")"
signals="$(curl -fsS "$BASE_URL/api/signals?limit=1")"
root_headers="$(curl -fsSI "$BASE_URL/")"
root_html="$(curl -fsS "$BASE_URL/")"
system_html="$(curl -fsS "$BASE_URL/system")"
method_html="$(curl -fsS "$BASE_URL/methodology")"
etag="$(curl -fsS -D - -o /dev/null "$BASE_URL/api/version" | tr -d '\r' | awk 'tolower($1)=="etag:" {print $2; exit}')"
edge_arch="$(printf '%s\n' "$root_headers" | tr -d '\r' | awk 'tolower($1)=="x-edge-architecture:" {print $2; exit}')"

candidate="$(curl -fsS "$BASE_URL/api/candidates?limit=1&min_hcs=0.01")"
identifier="$(python3 - "$candidate" <<'PY'
import json, sys
p=json.loads(sys.argv[1]); rows=p.get('results') or []
if rows: print(rows[0].get('ticker') or rows[0].get('cik') or '')
PY
)"
company_html=''
if [ -n "$identifier" ]; then company_html="$(curl -fsS "$BASE_URL/company/$identifier")"; fi

python3 - "$EXPECTED_VERSION" "$health" "$version" "$build" "$system" "$freshness" "$signals" "$etag" "$edge_arch" "$root_html" "$system_html" "$method_html" "$company_html" <<'PY'
import json, sys
expected, health_raw, version_raw, build_raw, system_raw, freshness_raw, signals_raw, etag, edge_arch, root_html, system_html, method_html, company_html = sys.argv[1:]
health=json.loads(health_raw); version=json.loads(version_raw); build=json.loads(build_raw); system=json.loads(system_raw); freshness=json.loads(freshness_raw); signals=json.loads(signals_raw)
assert health.get('status') in {'ok','degraded','error'}, health
assert health.get('operational_state') in {'operational','delayed','degraded','failed'}, health
assert health.get('version') == expected
assert version.get('version') == expected
assert build.get('version') == expected
versions={health.get('version'),version.get('version'),build.get('version'),system.get('version'),freshness.get('version')}
assert versions == {expected}, versions
schemas={health.get('api_schema_version'),version.get('api_schema_version'),build.get('api_schema_version'),system.get('meta',{}).get('api_schema_version'),freshness.get('meta',{}).get('api_schema_version')}
assert len(schemas)==1 and None not in schemas, schemas
assert etag.startswith('W/"'), etag
assert edge_arch == 'public-js-v1', edge_arch
assert 'data-page="home"' in root_html and 'data-ssr-rendered="true"' in root_html
assert 'Checking data freshness' not in root_html, 'SSR freshness placeholder leaked to production'
assert 'id="home-kpis"' in root_html and 'Highest current HCS' in root_html
assert 'Published signals</span><strong' in root_html and 'Threshold HCS ≥ 80' in root_html
assert '/favicon.svg' in root_html
assert 'id="view-company"' not in root_html and 'id="view-system"' not in root_html and 'id="view-methodology"' not in root_html
assert 'data-page="system"' in system_html and 'pipeline-flow' in system_html
assert 'id="system-refresh"' in system_html and 'id="system-webchecks"' in system_html
assert 'build-card' in system_html
assert 'data-page="methodology"' in method_html
for page in (root_html, system_html, method_html, company_html or root_html):
    assert 'The information provided on this website is for informational and educational purposes only' in page
if company_html:
    assert 'data-page="company"' in company_html and 'data-ssr-rendered="true"' in company_html
assert 'recent_cron_runs' in system and 'query_metrics_today' in system and 'pipeline_trends_7d' in system
assert 'backlog_history_24h' in system and 'deployment_history' in system and 'active_alerts' in system
assert 'results' in signals and 'meta' in signals
lf=freshness.get('latest_filing_at')
assert not lf or (len(lf)==10 and lf[4]=='-' and lf[7]=='-'), lf
print(f'Production verification passed for v{expected}')
PY
