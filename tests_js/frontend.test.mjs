import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const app = readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../public/index.html', import.meta.url), 'utf8');
const css = readFileSync(new URL('../public/style.css', import.meta.url), 'utf8');
const systemHtml = readFileSync(new URL('../public/system.html', import.meta.url), 'utf8');
const companyHtml = readFileSync(new URL('../public/company.html', import.meta.url), 'utf8');
const methodologyHtml = readFileSync(new URL('../public/methodology.html', import.meta.url), 'utf8');
const edge = readFileSync(new URL('../src/public_worker.js', import.meta.url), 'utf8');

test('footer version is loaded from the API', () => {
  assert.match(html, /id="footer-version"/);
  assert.match(app, /fetchJson\('\/api\/version'\)/);
});

test('research controls include filtering and reset behavior', () => {
  for (const id of ['filter-family', 'filter-industry', 'filter-min-hcs', 'filter-sort', 'filter-reset']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(app, /setupFilters/);
  assert.match(app, /filterQuery/);
});

test('public UI has safe source URL handling and no inline style mutation', () => {
  assert.match(app, /function safeSecUrl/);
  assert.doesNotMatch(app, /\.style\s*=/);
  assert.doesNotMatch(html, /style="/);
});

test('loading, retry and company timeline states are present', () => {
  assert.match(css, /\.skeleton-card/);
  assert.match(app, /data-retry/);
  assert.match(app, /Signal timeline/);
  assert.match(app, /Why now\?/);
});

test('v0.11 filters are shareable and browser history aware', () => {
  assert.match(app, /function syncFiltersToUrl/);
  assert.match(app, /function applyFiltersFromUrl/);
  assert.match(app, /popstate/);
  assert.match(app, /URLSearchParams/);
});

test('v0.11 autocomplete supports keyboard and aria navigation', () => {
  assert.match(app, /ArrowDown/);
  assert.match(app, /ArrowUp/);
  assert.match(app, /aria-activedescendant/);
  assert.match(app, /aria-expanded/);
});

test('v0.11 company history uses svg and explains score changes', () => {
  assert.match(app, /renderHistorySvg/);
  assert.match(app, /<svg/);
  assert.match(app, /Why did the score change\?/);
  assert.match(app, /today/);
});

test('v0.11 system console and operator diagnostics are present', () => {
  assert.match(systemHtml, /id="view-system"/);
  assert.match(systemHtml, /Operator diagnostics/);
  assert.match(app, /loadSystemConsole/);
  assert.match(app, /setupAdminDiagnostics/);
  assert.match(app, /\/api\/admin\/replay\/filing/);
});

test('v0.11 footer has one canonical version element and pipeline diagnostics collapse', () => {
  const versions = html.match(/id="footer-version"/g) || [];
  assert.equal(versions.length, 1);
  assert.match(html, /<details class="pipeline-details">/);
  assert.doesNotMatch(app, /freshness\.version/);
});

test('v0.12 build identity is rendered in the footer and fetched without browser cache', () => {
  assert.match(app, /fetchJson\('\/api\/build'\)/);
  assert.match(app, /cache: 'no-store'/);
  assert.match(app, /build \$\{sha\}/);
  assert.match(html, /Hidden Conviction v0\.(?:13\.[01]|14\.0)/);
});

test('v0.12 system console includes SEC access and drill-down details', () => {
  assert.match(app, /sec: 'SEC Access'/);
  assert.match(app, /oldest_pending_age_minutes/);
  assert.match(app, /latest_latency_seconds/);
  assert.match(app, /stage-drilldown/);
});

test('v0.12 browser history uses explicit window global', () => {
  assert.match(app, /window\.history\[method\]/);
  assert.doesNotMatch(app, /\n\s*history\[method\]/);
});

test('v0.12 SSR support styles are packaged', () => {
  assert.match(css, /v0\.12\.0 SSR/);
  assert.match(css, /\.ssr-history-text/);
  assert.match(css, /\.system-stage summary/);
});


test('v0.13 page documents are separate and route-specific', () => {
  assert.match(html, /id="view-home"/);
  assert.doesNotMatch(html, /id="view-company"/);
  assert.doesNotMatch(html, /id="view-system"/);
  assert.match(companyHtml, /id="view-company"/);
  assert.match(systemHtml, /id="view-system"/);
  assert.match(methodologyHtml, /id="view-methodology"/);
});

test('v0.13 disclaimer is centered and present on every page', () => {
  const phrase = /informational and educational purposes only/;
  for (const page of [html, companyHtml, systemHtml, methodologyHtml]) {
    assert.match(page, phrase);
    assert.match(page, /class="site-disclaimer"/);
  }
  assert.match(css, /\.site-disclaimer/);
  assert.match(css, /text-align: center/);
});

test('v0.13 edge worker proxies API and renders home directly from D1', () => {
  assert.match(edge, /env\.BACKEND\.fetch/);
  assert.match(edge, /env\.DB\.prepare\(signalSql\)/);
  assert.match(edge, /public-js-v1/);
  assert.match(edge, /X-Deployment-ID/);
});


test('v0.13.1 polish includes freshness, health clarity, favicon, copy links and system checks', () => {
  assert.match(html, /id="home-kpis"/);
  assert.match(html, /id="data-delay-badge"/);
  assert.match(html, /href="\/system" id="system-status"/);
  assert.match(systemHtml, /id="system-refresh"/);
  assert.match(systemHtml, /id="system-webchecks"/);
  assert.match(app, /function relativeTime/);
  assert.match(app, /filing backlog/);
  assert.match(app, /data-copy-url/);
  assert.match(app, /loadWebChecks/);
  assert.match(edge, /displayDate/);
  assert.match(edge, /build-card/);
});

test('v0.14 operational polish separates KPI labels and adds adaptive telemetry', () => {
  assert.match(edge, /Published signals<\/span><strong>/);
  assert.match(edge, /Threshold HCS ≥ 80/);
  assert.match(edge, /backlogTrendSvg/);
  assert.match(edge, /Estimated clearance/);
  assert.match(app, /operational_state/);
  assert.match(app, /admin-replay-all/);
  assert.match(css, /grid-template-columns: minmax\(0,1fr\) auto/);
});
