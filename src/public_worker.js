import { APP_VERSION, API_SCHEMA_VERSION, BUILD_SHA, BUILD_DEPLOYED_AT, BUILD_ID } from './build_meta.js';

const PUBLIC_HCS_THRESHOLD = 80;
const CRITICAL_NO_STORE = new Set(['/api/health', '/api/version', '/api/build', '/api/system', '/api/freshness']);

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}


function displayDate(value) {
  if (!value) return '';
  const raw = String(value).trim();
  const normalized = /^\d{8}$/.test(raw) ? `${raw.slice(0,4)}-${raw.slice(4,6)}-${raw.slice(6,8)}` : raw.slice(0,10);
  const d = new Date(`${normalized}T12:00:00Z`);
  if (Number.isNaN(d.getTime())) return normalized;
  return new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(d);
}

function displayTimestamp(value) {
  if (!value) return '';
  const raw = String(value);
  const d = new Date(raw.includes('T') ? raw : `${raw.replace(' ', 'T')}Z`);
  if (Number.isNaN(d.getTime())) return raw;
  return new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York', timeZoneName: 'short' }).format(d);
}

function numberText(value) {
  const n = Number(value);
  return Number.isFinite(n) ? new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(n) : String(value ?? '');
}

function stageValue(field, value) {
  if (value == null || value === '') return '';
  if (field.endsWith('_at')) return displayTimestamp(value);
  if (field.endsWith('_date') || field.includes('_through')) return displayDate(value);
  if (typeof value === 'number' && Number.isInteger(value)) return numberText(value);
  return String(value);
}
function cleanRoute(value) {
  return String(value ?? '').replace(/[^A-Za-z0-9._-]/g, '');
}

function securityHeaders(headers = new Headers()) {
  headers.set('X-Content-Type-Options', 'nosniff');
  headers.set('X-Frame-Options', 'DENY');
  headers.set('Referrer-Policy', 'strict-origin-when-cross-origin');
  headers.set('Permissions-Policy', 'camera=(), microphone=(), geolocation=()');
  headers.set('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'");
  headers.set('X-Hidden-Conviction-Version', APP_VERSION);
  headers.set('X-API-Schema-Version', API_SCHEMA_VERSION);
  headers.set('X-Hidden-Conviction-Build', BUILD_ID);
  headers.set('X-Deployment-ID', BUILD_ID);
  headers.set('X-Edge-Architecture', 'public-js-v1');
  return headers;
}

async function sha256Etag(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const hex = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 24);
  return `W/"${hex}"`;
}

async function htmlResponse(request, html, { status = 200, dbMs = null } = {}) {
  const etag = await sha256Etag(html);
  if (request.headers.get('if-none-match') === etag) {
    return new Response(null, { status: 304, headers: securityHeaders(new Headers({ ETag: etag })) });
  }
  const headers = securityHeaders(new Headers({
    'Content-Type': 'text/html; charset=utf-8',
    'Cache-Control': 'public, max-age=30, stale-while-revalidate=120',
    'ETag': etag,
  }));
  if (dbMs != null) headers.set('Server-Timing', `edge-db;dur=${dbMs.toFixed(1)}`);
  return new Response(request.method === 'HEAD' ? null : html, { status, headers });
}

async function assetText(env, name) {
  const response = await env.ASSETS.fetch(new Request(`https://assets.local/${name}`));
  if (!response.ok) throw new Error(`Missing asset template ${name}`);
  return response.text();
}

function withMetadata(text, { title, description, canonical }) {
  text = text.replace(/<title>.*?<\/title>/s, `<title>${esc(title)}</title>`);
  text = text.replace(/<meta name="description" content="[^"]*">/, `<meta name="description" content="${esc(description)}">`);
  const rich = `<link rel="canonical" href="${esc(canonical)}">\n  <meta property="og:title" content="${esc(title)}">\n  <meta property="og:description" content="${esc(description)}">\n  <meta property="og:url" content="${esc(canonical)}">\n  <meta property="og:type" content="website">`;
  return text.replace('</head>', `  ${rich}\n</head>`);
}

function injectBuild(text) {
  return text.replace(/<span id="footer-version">.*?<\/span>/s,
    `<span id="footer-version">Hidden Conviction v${esc(APP_VERSION)} · build ${esc(BUILD_SHA)}</span>`);
}

async function footerFreshness(env) {
  try {
    const row = await env.DB.prepare(`
      SELECT
        (SELECT MAX(CASE WHEN LENGTH(filed_at)=8 AND filed_at NOT LIKE '%-%' THEN SUBSTR(filed_at,1,4)||'-'||SUBSTR(filed_at,5,2)||'-'||SUBSTR(filed_at,7,2) ELSE SUBSTR(filed_at,1,10) END) FROM filings) AS latest_filing_at,
        (SELECT MAX(score_date) FROM scores) AS latest_score_date,
        (SELECT MAX(created_at) FROM scores) AS latest_score_created_at
    `).first();
    const parts = [];
    if (row?.latest_filing_at) parts.push(`SEC filing coverage through ${esc(displayDate(row.latest_filing_at))}`);
    if (row?.latest_score_date) parts.push(`Scores through ${esc(displayDate(row.latest_score_date))}`);
    parts.push(`deployed ${esc(displayTimestamp(BUILD_DEPLOYED_AT))}`);
    return parts.join(' · ');
  } catch (_) {
    return `Build ${esc(BUILD_ID)}`;
  }
}

async function injectFooterFreshness(text, env) {
  const value = await footerFreshness(env);
  return text.replace(/<div id="footer-freshness" class="footer-freshness">.*?<\/div>/s,
    `<div id="footer-freshness" class="footer-freshness">${value}</div>`);
}

function signalFamilyClause(family) {
  const mapping = {
    insider: '(s.insider_score > 0 OR s.cluster_score > 0)',
    ownership: 's.ownership_score > 0',
    institutional: 's.whale_score > 0',
    event: 's.event_score <> 0',
    capital_allocation: 's.capital_allocation_score > 0',
  };
  return mapping[family] || '1=1';
}

function orderClause(sort) {
  if (sort === 'change') return 'ABS(COALESCE(s.score_delta,0)) DESC, s.hcs_score DESC';
  if (sort === 'recent') return 's.created_at DESC, s.hcs_score DESC';
  if (sort === 'ticker') return 'COALESCE(c.ticker,c.company_name,s.cik) ASC, s.hcs_score DESC';
  return 's.hcs_score DESC, COALESCE(c.ticker,c.company_name,s.cik) ASC';
}

function scoreCard(row) {
  const identifier = row.ticker || row.cik || 'Unknown';
  const company = row.company_name || '';
  const route = cleanRoute(row.ticker || row.cik || '');
  const score = Math.round(Number(row.hcs_score || 0));
  const delta = Number(row.score_delta || 0);
  const deltaText = delta ? `${delta > 0 ? '+' : ''}${delta.toFixed(1)} today` : 'unchanged today';
  const badges = [];
  if (Number(row.insider_score || 0) > 0 || Number(row.cluster_score || 0) > 0) badges.push('Insider');
  if (Number(row.ownership_score || 0) > 0) badges.push('Ownership');
  if (Number(row.whale_score || 0) > 0) badges.push('Institutional');
  if (Number(row.event_score || 0) !== 0) badges.push('Event');
  const badgeHtml = badges.length ? `<div class="signal-badges">${badges.map(x => `<span class="signal-badge">${esc(x)}</span>`).join('')}</div>` : '';
  return `<a class="card card-link ssr-card" href="/company/${encodeURIComponent(route)}">
    <div class="identity"><strong>${esc(identifier)}</strong><div class="muted">${esc(company)}</div>${row.industry ? `<div class="industry-tag">${esc(row.industry)}${row.sic ? ` · SIC ${esc(row.sic)}` : ''}</div>` : ''}${badgeHtml}</div>
    <div class="score-block"><div class="score">${score}</div><div class="score-label" title="HCS means Hidden Conviction Score">HCS</div><div class="muted small">${esc(deltaText)}</div></div>
    <div class="card-meta"><div class="classification">${esc(row.classification || '')}</div></div>
  </a>`;
}

async function recordQueryMetric(env, ctx, name, durationMs, rows) {
  if (!env.DB) return;
  const slow = durationMs >= 75 ? 1 : 0;
  const work = env.DB.prepare(`
    INSERT INTO query_metrics(query_name, metric_date, executions, rows_returned, total_duration_ms, max_duration_ms, slow_count, updated_at)
    VALUES (?, DATE('now'), 1, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(query_name, metric_date) DO UPDATE SET
      executions=query_metrics.executions+1,
      rows_returned=query_metrics.rows_returned+excluded.rows_returned,
      total_duration_ms=query_metrics.total_duration_ms+excluded.total_duration_ms,
      max_duration_ms=MAX(query_metrics.max_duration_ms, excluded.max_duration_ms),
      slow_count=query_metrics.slow_count+excluded.slow_count,
      updated_at=CURRENT_TIMESTAMP
  `).bind(name, Number(rows || 0), durationMs, durationMs, slow).run().catch(() => null);
  if (ctx?.waitUntil) ctx.waitUntil(work); else await work;
}

async function renderHome(request, env, ctx) {
  let text = await assetText(env, 'index.html');
  const url = new URL(request.url);
  const family = (url.searchParams.get('family') || '').trim().toLowerCase();
  const industry = (url.searchParams.get('industry') || '').trim().slice(0, 80);
  const minHcs = Math.max(0, Math.min(100, Number(url.searchParams.get('min_hcs') || 0) || 0));
  const sort = (url.searchParams.get('sort') || 'score').trim().toLowerCase();
  const familyClause = signalFamilyClause(family);
  const industryClause = industry ? `AND (UPPER(COALESCE(c.industry,'')) LIKE ? OR UPPER(COALESCE(c.sic,'')) LIKE ?)` : '';
  const order = orderClause(sort);
  const baseSelect = `SELECT s.cik, c.ticker, c.company_name, c.sic, c.industry, s.hcs_score, s.classification, s.score_delta,
    s.insider_score, s.cluster_score, s.ownership_score, s.whale_score, s.event_score
    FROM scores s JOIN companies c ON c.cik=s.cik
    WHERE c.hcs_eligible=1 AND s.score_date=DATE('now') AND ${familyClause}`;
  const like = `%${industry.toUpperCase()}%`;
  const signalSql = `${baseSelect} AND s.hcs_score>=? ${industryClause} ORDER BY ${order} LIMIT 20`;
  const candidateSql = `${baseSelect} AND s.hcs_score>? AND s.hcs_score<? ${industryClause} ORDER BY ${order} LIMIT 10`;
  const signalBinds = [Math.max(PUBLIC_HCS_THRESHOLD, minHcs), ...(industry ? [like, like] : [])];
  const candidateBinds = [minHcs, PUBLIC_HCS_THRESHOLD, ...(industry ? [like, like] : [])];
  const started = performance.now();
  const [signalResult, candidateResult, homeMeta] = await Promise.all([
    env.DB.prepare(signalSql).bind(...signalBinds).all(),
    env.DB.prepare(candidateSql).bind(...candidateBinds).all(),
    env.DB.prepare(`SELECT
      (SELECT MAX(CASE WHEN LENGTH(filed_at)=8 AND filed_at NOT LIKE '%-%' THEN SUBSTR(filed_at,1,4)||'-'||SUBSTR(filed_at,5,2)||'-'||SUBSTR(filed_at,7,2) ELSE SUBSTR(filed_at,1,10) END) FROM filings) AS latest_filing_at,
      (SELECT MAX(created_at) FROM filings) AS latest_discovered_at,
      (SELECT MAX(score_date) FROM scores) AS latest_score_date,
      (SELECT MAX(hcs_score) FROM scores s2 JOIN companies c2 ON c2.cik=s2.cik WHERE c2.hcs_eligible=1 AND s2.score_date=DATE('now')) AS highest_hcs,
      (SELECT COUNT(*) FROM scores s3 JOIN companies c3 ON c3.cik=s3.cik WHERE c3.hcs_eligible=1 AND s3.score_date=DATE('now') AND s3.hcs_score>=80) AS published_signals
    `).first(),
  ]);
  const dbMs = performance.now() - started;
  const signals = signalResult.results || [];
  const candidates = candidateResult.results || [];
  recordQueryMetric(env, ctx, 'edge_home', dbMs, signals.length + candidates.length);
  const signalHtml = signals.length ? signals.map(scoreCard).join('') : '<div class="empty"><strong>No companies currently meet the HCS ≥ 80 threshold.</strong><span>The publication threshold remains strict even when the list is empty.</span></div>';
  const candidateHtml = candidates.length ? candidates.map(scoreCard).join('') : '<div class="empty">No below-threshold activity candidates are currently available.</div>';
  text = text.replace(/<!-- SSR_SIGNALS -->.*?<!-- \/SSR_SIGNALS -->/s, `<!-- SSR_SIGNALS -->${signalHtml}<!-- /SSR_SIGNALS -->`);
  text = text.replace(/<!-- SSR_CANDIDATES -->.*?<!-- \/SSR_CANDIDATES -->/s, `<!-- SSR_CANDIDATES -->${candidateHtml}<!-- /SSR_CANDIDATES -->`);
  text = text.replace('<span id="status">Loading...</span>', `<span id="status">${signals.length} live signal${signals.length === 1 ? '' : 's'}</span>`);
  text = text.replace('<span id="candidate-status">Loading...</span>', `<span id="candidate-status">${candidates.length} active candidate${candidates.length === 1 ? '' : 's'}</span>`);
  const freshnessText = homeMeta?.latest_filing_at ? `SEC filing coverage through ${esc(displayDate(homeMeta.latest_filing_at))}${homeMeta.latest_score_date ? ` · Scores through ${esc(displayDate(homeMeta.latest_score_date))}` : ''}` : 'SEC filing coverage status unavailable';
  text = text.replace('<div class="freshness" id="home-freshness">Checking data freshness…</div>', `<div class="freshness" id="home-freshness">${freshnessText}</div>`);
  const highest = Math.round(Number(homeMeta?.highest_hcs || 0));
  const published = Number(homeMeta?.published_signals || 0);
  const kpis = `<div class="home-kpi"><span>Highest current HCS</span><strong title="HCS means Hidden Conviction Score">${highest}</strong></div><div class="home-kpi"><span>Published signals</span><strong>${numberText(published)}</strong><small>Threshold HCS ≥ 80</small></div>`;
  text = text.replace(/<!-- SSR_KPIS -->.*?<!-- \/SSR_KPIS -->/s, `<!-- SSR_KPIS -->${kpis}<!-- /SSR_KPIS -->`);
  text = text.replace('<body data-page="home">', '<body data-page="home" data-ssr-rendered="true">');
  text = injectBuild(text);
  text = await injectFooterFreshness(text, env);
  text = withMetadata(text, { title: 'Hidden Conviction', description: 'Filing-driven public-market conviction signals built from public SEC data.', canonical: `${url.origin}/` });
  return htmlResponse(request, text, { dbMs });
}

function safeSecUrl(value) {
  const url = String(value || '');
  return url.startsWith('https://www.sec.gov/') || url.startsWith('https://sec.gov/') ? url : '';
}

function renderCompanyPayload(payload, identifier) {
  const company = payload.company || {};
  const score = payload.score || {};
  const hcs = Math.round(Number(score.hcs_score || 0));
  const signals = payload.signals || [];
  const evidence = payload.evidence || [];
  const history = payload.score_history || [];
  const signalHtml = signals.length ? signals.map(item => `<div class="method-card"><strong>${esc(item.label || item.key || 'Signal')}</strong><p>Active public signal family.</p></div>`).join('') : '<div class="empty compact">No active public signal families.</div>';
  const evidenceHtml = evidence.length ? evidence.slice(0, 12).map(item => {
    const link = safeSecUrl(item.filing_url);
    return `<div class="evidence-row"><div><strong>${esc(item.form_type || item.family || 'SEC filing')}</strong><span>${esc(displayDate(item.event_date || item.filed_at || ''))}</span></div><div class="source-actions">${link ? `<a href="${esc(link)}" rel="noopener noreferrer">Open SEC filing</a><button class="copy-source" type="button" data-copy-url="${esc(link)}">Copy link</button>` : 'SEC filing'}</div></div>`;
  }).join('') : '<div class="empty compact">No active source evidence.</div>';
  const historyText = history.slice(0, 15).reverse().map(item => `${esc(item.score_date || '')}: ${Math.round(Number(item.hcs_score || 0))}`).join(' · ') || 'No score history available.';
  const delta = Number(score.score_delta || 0);
  return `<nav class="breadcrumb" aria-label="Breadcrumb"><a href="/">Signals</a><span>›</span><span>${esc(company.ticker || company.cik || identifier)}</span><span>›</span><span>${esc(company.company_name || identifier)}</span></nav><header class="company-hero" data-ssr-rendered="true"><div><div class="eyebrow">${esc(company.ticker || identifier)}</div><h1 class="page-title">${esc(company.company_name || identifier)}</h1><p class="lede">${esc(company.exchange || '')}${company.sic ? ` · SIC ${esc(company.sic)}` : ''}${company.industry ? ` · ${esc(company.industry)}` : ''}</p></div><div class="hero-score"><div class="score giant">${hcs}</div><div class="score-label" title="HCS means Hidden Conviction Score">HCS</div><div class="classification">${esc(score.classification || '')}</div><div class="score-delta">${delta > 0 ? '+' : ''}${delta.toFixed(1)} today</div></div></header>
    <div class="freshness company-freshness">Score dated ${esc(displayDate(score.score_date) || 'not available')}</div>
    ${payload.why_now ? `<div class="why-now"><strong>Why now?</strong> ${esc(payload.why_now)}</div>` : ''}
    ${payload.why_changed ? `<div class="why-changed"><strong>Why did the score change?</strong> ${esc(payload.why_changed)}</div>` : ''}
    <section class="detail-section"><div class="section-head detail-head"><div><h2>Why this score?</h2><p class="section-copy">Active signal families from the current materialized score.</p></div></div><div class="signal-chip-grid">${signalHtml}</div></section>
    <section class="detail-section"><div class="section-head detail-head"><div><h2>Source evidence</h2><p class="section-copy">Public SEC filings supporting the active signal families.</p></div></div><div class="evidence-list">${evidenceHtml}</div></section>
    <section class="detail-section"><div class="section-head detail-head"><div><h2>Score history</h2></div></div><div class="ssr-history-text">${historyText}</div></section>`;
}

async function backendJson(env, path, request) {
  const original = new URL(request.url);
  const target = new URL(path, original.origin);
  const proxy = new Request(target.toString(), { method: 'GET', headers: { Accept: 'application/json', 'X-Request-ID': request.headers.get('x-request-id') || crypto.randomUUID() } });
  const response = await env.BACKEND.fetch(proxy);
  if (!response.ok) return { response, payload: null };
  return { response, payload: await response.json() };
}

async function renderCompany(request, env) {
  const url = new URL(request.url);
  const identifier = decodeURIComponent(url.pathname.split('/').slice(2).join('/')).slice(0, 80);
  let text = await assetText(env, 'company.html');
  const { response, payload } = await backendJson(env, `/api/company/${encodeURIComponent(identifier)}`, request);
  let status = response.status;
  if (payload) {
    text = text.replace(/<!-- SSR_COMPANY -->.*?<!-- \/SSR_COMPANY -->/s, `<!-- SSR_COMPANY -->${renderCompanyPayload(payload, identifier)}<!-- /SSR_COMPANY -->`);
    const company = payload.company || {};
    const score = payload.score || {};
    const title = `${company.ticker || identifier} · ${company.company_name || identifier} · Hidden Conviction`;
    const description = `Hidden Conviction research for ${company.company_name || identifier}: current HCS ${Math.round(Number(score.hcs_score || 0))}, with SEC filing evidence and score history.`;
    text = withMetadata(text, { title, description, canonical: `${url.origin}/company/${encodeURIComponent(identifier)}` });
  } else {
    const notFound = `<div class="not-found"><div class="eyebrow">NOT FOUND</div><h1 class="page-title">Company not found.</h1><p class="lede">No issuer matched ${esc(identifier)}.</p></div>`;
    text = text.replace(/<!-- SSR_COMPANY -->.*?<!-- \/SSR_COMPANY -->/s, `<!-- SSR_COMPANY -->${notFound}<!-- /SSR_COMPANY -->`);
    text = withMetadata(text, { title: 'Company not found · Hidden Conviction', description: 'The requested company could not be found in Hidden Conviction.', canonical: `${url.origin}/company/${encodeURIComponent(identifier)}` });
    status = 404;
  }
  text = text.replace('<body data-page="company">', '<body data-page="company" data-ssr-rendered="true">');
  text = injectBuild(text);
  text = await injectFooterFreshness(text, env);
  return htmlResponse(request, text, { status });
}

function backlogTrendSvg(history) {
  const rows = (history || []).slice(-72);
  if (rows.length < 2) return '<div class="empty compact">Backlog history will appear after two cron samples.</div>';
  const values = rows.map(row => Math.max(0, Number(row.pending || 0)));
  const max = Math.max(1, ...values);
  const points = values.map((value, i) => {
    const x = rows.length === 1 ? 0 : (i / (rows.length - 1)) * 100;
    const y = 36 - (value / max) * 32;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const first = displayTimestamp(rows[0]?.captured_at || '');
  const last = displayTimestamp(rows[rows.length - 1]?.captured_at || '');
  return `<div class="backlog-chart"><svg viewBox="0 0 100 40" role="img" aria-label="Filing backlog trend"><polyline points="${points}" fill="none" vector-effect="non-scaling-stroke"></polyline></svg><div class="chart-axis"><span>${esc(first)}</span><strong>${numberText(values[values.length - 1])} pending</strong><span>${esc(last)}</span></div></div>`;
}

function renderSystem(payload) {
  const stages = payload?.stages || {};
  const order = ['sec', 'discovery', 'queue', 'processing', 'identity', 'scoring', 'public_api'];
  const flow = `<div class="pipeline-flow" aria-label="Pipeline flow">${order.map(key => `<div class="pipeline-node ${esc(stages[key]?.status || 'unknown')}">${esc(key.replaceAll('_', ' '))}</div>`).join('')}</div>`;
  const details = Object.entries(stages).map(([key, stage]) => {
    const fields = Object.entries(stage || {}).filter(([field, value]) => field !== 'status' && value != null && value !== '').slice(0, 14)
      .map(([field, value]) => `<div><span>${esc(field.replaceAll('_', ' '))}</span><strong>${typeof value === 'object' ? esc(JSON.stringify(value)) : esc(stageValue(field, value))}</strong></div>`).join('');
    return `<details class="system-stage ${esc(stage?.status || 'unknown')}" data-ssr-rendered="true"><summary><strong>${esc(key.replaceAll('_', ' '))}</strong><span class="stage-state">${esc(String(stage?.status || 'unknown').toUpperCase())}</span></summary><div class="stage-drilldown">${fields || '<div>No additional public detail.</div>'}</div></details>`;
  }).join('');
  const cron = (payload.recent_cron_runs || []).map(row => `<div class="ops-row"><strong>${esc(row.status || '')}</strong><span>${esc(displayTimestamp(row.started_at || ''))}</span><span>${esc(numberText(row.duration_ms ?? ''))} ms</span></div>`).join('') || '<div class="empty compact">No cron history available.</div>';
  const errors = (payload.recent_error_summary || []).map(row => `<div class="ops-row"><strong>${esc(row.pipeline || row.kind || '')}</strong><span>${esc(numberText(row.count || 0))} recent</span><span>${esc(displayTimestamp(row.latest_at || ''))}</span></div>`).join('') || '<div class="empty compact">No recent pipeline errors.</div>';
  const trends = (payload.pipeline_trends_7d || []).slice(0, 42).map(row => `<div class="ops-row"><strong>${esc(row.metric_date || '')} · ${esc(row.pipeline || '')}</strong><span>${esc(numberText(row.processed || 0))} processed / ${esc(numberText(row.failed || 0))} failed</span><span>${esc(row.latest_latency_seconds || 0)}s latency</span></div>`).join('') || '<div class="empty compact">No seven day pipeline telemetry available.</div>';
  const queries = (payload.query_metrics_today || []).slice(0, 12).map(row => `<div class="ops-row"><strong>${esc(row.query_name || '')}</strong><span>${esc(numberText(row.executions || 0))} executions / ${esc(numberText(row.slow_count || 0))} slow</span><span>${esc(row.max_duration_ms || 0)} ms max</span></div>`).join('') || '<div class="empty compact">No query timing telemetry available.</div>';
  const alerts = (payload.active_alerts || []).map(row => `<div class="alert-row ${esc(row.severity || 'warning')}"><strong>${esc(row.message || row.code || 'Alert')}</strong><span>${esc(displayTimestamp(row.last_seen_at || ''))}</span></div>`).join('') || '<div class="empty compact">No active operational alerts.</div>';
  const deploymentRows = payload.deployment_history || [];
  const deployments = deploymentRows.map(row => `<div class="ops-row"><strong>v${esc(row.app_version || '')}</strong><span>home ${Number(row.home_ttfb_ms || 0).toFixed(0)} ms · health ${Number(row.health_ttfb_ms || 0).toFixed(0)} ms · system ${Number(row.system_ttfb_ms || 0).toFixed(0)} ms</span><span>${esc(displayTimestamp(row.captured_at || row.deployed_at || ''))}</span></div>`).join('') || '<div class="empty compact">Performance history will appear after the next verified deployment.</div>';
  let deploymentComparison = '<div class="empty compact">A version-over-version comparison will appear after two deployment receipts.</div>';
  if (deploymentRows.length >= 2) {
    const current = deploymentRows[0];
    const previous = deploymentRows[1];
    const delta = key => Number(current[key] || 0) - Number(previous[key] || 0);
    const signed = value => `${value > 0 ? '+' : ''}${value.toFixed(0)} ms`;
    deploymentComparison = `<div class="deployment-compare"><strong>v${esc(current.app_version || '')} vs v${esc(previous.app_version || '')}</strong><span>home ${signed(delta('home_ttfb_ms'))} · health ${signed(delta('health_ttfb_ms'))} · system ${signed(delta('system_ttfb_ms'))}</span><span>queue ${numberText(current.queue_pending || 0)} vs ${numberText(previous.queue_pending || 0)} pending</span></div>`;
  }
  const queue = stages.queue || {};
  const controller = queue.controller || {};
  const eta = queue.estimated_clear_minutes == null ? 'Not currently draining' : queue.estimated_clear_minutes <= 0 ? 'Clear' : `${Math.round(Number(queue.estimated_clear_minutes))} min`;
  const throughput = `<div class="throughput-grid">
    <div><span>Discovered / hour</span><strong>${numberText(queue.discovered_per_hour || 0)}</strong></div>
    <div><span>Processed / hour</span><strong>${numberText(queue.processed_per_hour || 0)}</strong></div>
    <div><span>Net backlog / hour</span><strong>${Number(queue.net_backlog_per_hour || 0) > 0 ? '+' : ''}${numberText(queue.net_backlog_per_hour || 0)}</strong></div>
    <div><span>Estimated clearance</span><strong>${esc(eta)}</strong></div>
    <div><span>Controller</span><strong>${esc(controller.mode || 'steady')}</strong></div>
    <div><span>Target concurrency</span><strong>${numberText(controller.target_concurrency || 1)}</strong></div>
  </div>`;
  const buildCard = `<div class="build-card" aria-label="Build information"><div><span>Version</span><strong>v${esc(APP_VERSION)}</strong></div><div><span>Build</span><strong>${esc(BUILD_SHA)}</strong></div><div><span>Deployed</span><strong>${esc(displayTimestamp(BUILD_DEPLOYED_AT))}</strong></div></div>`;
  const state = payload.operational_state || payload.status || 'unknown';
  return `${flow}${buildCard}<div id="system-overall" class="system-overall ${esc(state)}" aria-live="polite">Operational state: ${esc(String(state).toUpperCase())}</div>${throughput}<section class="ops-history"><h2>Active alerts</h2>${alerts}</section><section class="ops-history"><h2>24 hour backlog trend</h2>${backlogTrendSvg(payload.backlog_history_24h || [])}</section><div id="system-stages" class="system-stages" aria-live="polite">${details}</div><section class="ops-history"><h2>Recent cron runs</h2>${cron}</section><section class="ops-history"><h2>7 day pipeline activity</h2>${trends}</section><section class="ops-history"><h2>Query performance today</h2>${queries}</section><section class="ops-history"><h2>Deployment comparison</h2>${deploymentComparison}</section><section class="ops-history"><h2>Deployment performance</h2>${deployments}</section><section class="ops-history"><h2>Recent errors</h2>${errors}</section>`;
}

async function renderSystemPage(request, env) {
  const url = new URL(request.url);
  let text = await assetText(env, 'system.html');
  const { payload } = await backendJson(env, '/api/system', request);
  const body = payload ? renderSystem(payload) : '<div class="empty">System state unavailable.</div>';
  text = text.replace(/<!-- SSR_SYSTEM -->.*?<!-- \/SSR_SYSTEM -->/s, `<!-- SSR_SYSTEM -->${body}<!-- /SSR_SYSTEM -->`);
  text = text.replace('<body data-page="system">', '<body data-page="system" data-ssr-rendered="true">');
  text = injectBuild(text);
  text = await injectFooterFreshness(text, env);
  text = withMetadata(text, { title: 'System Console · Hidden Conviction', description: 'Live operational health for Hidden Conviction SEC discovery, processing, scoring, and publication pipelines.', canonical: `${url.origin}/system` });
  return htmlResponse(request, text);
}

async function renderMethodology(request, env) {
  const url = new URL(request.url);
  let text = await assetText(env, 'methodology.html');
  text = text.replace('<body data-page="methodology">', '<body data-page="methodology" data-ssr-rendered="true">');
  text = injectBuild(text);
  text = await injectFooterFreshness(text, env);
  text = withMetadata(text, { title: 'Methodology · Hidden Conviction', description: 'How Hidden Conviction interprets insider, beneficial ownership, institutional, and corporate-event SEC filing signals.', canonical: `${url.origin}/methodology` });
  return htmlResponse(request, text);
}

async function proxyBackend(request, env) {
  const response = await env.BACKEND.fetch(request);
  const headers = securityHeaders(new Headers(response.headers));
  const path = new URL(request.url).pathname;
  if (path.startsWith('/api/admin/') || CRITICAL_NO_STORE.has(path)) headers.set('Cache-Control', 'no-store');
  headers.set('X-Edge-Backend', 'hidden-conviction-ingest');
  return new Response(request.method === 'HEAD' ? null : response.body, { status: response.status, headers });
}

async function staticAsset(request, env) {
  const response = await env.ASSETS.fetch(request);
  const headers = securityHeaders(new Headers(response.headers));
  if (new URL(request.url).pathname.match(/\.(?:css|js|png|svg|ico)$/)) headers.set('Cache-Control', 'public, max-age=3600, stale-while-revalidate=86400');
  return new Response(request.method === 'HEAD' ? null : response.body, { status: response.status, headers });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, '') || '/';
    try {
      if (path.startsWith('/api/') || path === '/sitemap.xml') return proxyBackend(request, env);
      if (path === '/') return renderHome(request, env, ctx);
      if (path === '/system') return renderSystemPage(request, env);
      if (path === '/methodology') return renderMethodology(request, env);
      if (path.startsWith('/company/')) return renderCompany(request, env);
      return staticAsset(request, env);
    } catch (error) {
      console.log(JSON.stringify({ event: 'public_worker_error', path, error: String(error).slice(0, 500), build_id: BUILD_ID }));
      if (path.startsWith('/api/')) return proxyBackend(request, env);
      return new Response('Temporarily unavailable', { status: 503, headers: securityHeaders(new Headers({ 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' })) });
    }
  }
};
