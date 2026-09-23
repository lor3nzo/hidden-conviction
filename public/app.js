let freshnessSnapshot = null;

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function scoreValue(row) {
  const value = Number(row?.hcs_score || 0);
  return Number.isFinite(value) ? value : 0;
}

function displayIdentifier(row) {
  if (row?.ticker) return row.ticker;
  if (row?.cik) return `CIK ${row.cik}`;
  return 'Unknown';
}

function routeIdentifier(row) {
  return row?.ticker || row?.cik || '';
}

function prettyDate(value) {
  if (!value) return '';
  const raw = String(value).trim();
  const normalized = /^\d{8}$/.test(raw) ? `${raw.slice(0,4)}-${raw.slice(4,6)}-${raw.slice(6,8)}` : raw.slice(0,10);
  const d = new Date(`${normalized}T12:00:00Z`);
  if (Number.isNaN(d.getTime())) return normalized;
  return new Intl.DateTimeFormat('en-US', {
    month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC'
  }).format(d);
}

function relativeTime(value) {
  if (!value) return '';
  const d = new Date(String(value).replace(' ', 'T') + (String(value).includes('Z') || /[+-]\d\d:\d\d$/.test(String(value)) ? '' : 'Z'));
  if (Number.isNaN(d.getTime())) return '';
  const seconds = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function prettyTimestamp(value) {
  if (!value) return '';
  const d = new Date(String(value));
  if (Number.isNaN(d.getTime())) return String(value);
  return new Intl.DateTimeFormat('en-US', {
    month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit',
    timeZone: 'America/New_York', timeZoneName: 'short'
  }).format(d);
}

function compactNumber(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '';
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(n);
}

function money(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '';
  return new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', maximumFractionDigits: 0
  }).format(n);
}

function percent(value, digits = 2) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '';
  return `${n.toFixed(digits)}%`;
}

function safeSecUrl(value) {
  if (!value) return '';
  try {
    const url = new URL(String(value));
    if (url.protocol !== 'https:') return '';
    if (url.hostname !== 'www.sec.gov' && url.hostname !== 'sec.gov') return '';
    return url.toString();
  } catch (_) {
    return '';
  }
}

function renderCard(row, { showMeter = false } = {}) {
  const score = scoreValue(row);
  const identifier = escapeHtml(displayIdentifier(row));
  const company = escapeHtml(row.company_name || '');
  const classification = escapeHtml(row.classification || '');
  const scoreDate = escapeHtml(row.score_date || '');
  const route = encodeURIComponent(routeIdentifier(row));
  const meter = showMeter
    ? `<div class="meter" aria-label="HCS ${Math.round(score)} out of 100"><meter min="0" max="100" value="${Math.max(0, Math.min(100, score))}">${Math.round(score)}</meter><i></i></div>`
    : '';
  const badgeLabels = [];
  if (Number(row.has_insider || 0) > 0) badgeLabels.push('Insider');
  if (Number(row.has_ownership || 0) > 0) badgeLabels.push('Ownership');
  if (Number(row.has_institutional || 0) > 0) badgeLabels.push('Institutional');
  if (Number(row.has_event || 0) > 0) badgeLabels.push(Number(row.has_negative_event || 0) > 0 ? '8-K risk' : '8-K');
  if (Number(row.has_capital_allocation || 0) > 0) badgeLabels.push('Capital');
  const badges = badgeLabels.length ? `<div class="signal-badges">${badgeLabels.map(label => `<span class="signal-badge">${label}</span>`).join('')}</div>` : '';

  return `
    <a class="card card-link" href="/company/${route}" aria-label="View ${company || identifier}">
      <div class="identity">
        <strong>${identifier}</strong>
        <div class="muted">${company}</div>
        ${row.industry ? `<div class="industry-tag">${escapeHtml(row.industry)}${row.sic ? ` · SIC ${escapeHtml(row.sic)}` : ''}</div>` : ''}
        ${row.active_signal_count != null ? `<div class="signal-count">${Number(row.active_signal_count)} active signal ${Number(row.active_signal_count) === 1 ? 'family' : 'families'}</div>` : ''}
        ${badges}
        ${row.latest_evidence_date ? `<div class="muted small">Latest evidence ${escapeHtml(prettyDate(row.latest_evidence_date))}</div>` : ''}
        ${meter}
      </div>
      <div class="score-block">
        <div class="score">${Math.round(score)}</div>
        <div class="score-label" title="HCS means Hidden Conviction Score">HCS</div>
        ${row.one_day_change != null ? `<div class="muted small">${Number(row.one_day_change) > 0 ? '+' : ''}${Number(row.one_day_change).toFixed(1)} 1d</div>` : ''}
      </div>
      <div class="card-meta">
        <div class="classification">${classification}</div>
        ${scoreDate ? `<div class="muted small">${scoreDate}</div>` : ''}
      </div>
    </a>
  `;
}

async function fetchJson(url) {
  const response = await fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store' });
  if (!response.ok) {
    const error = new Error(`HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function loadAppVersion() {
  const el = document.getElementById('footer-version');
  try {
    const [version, build] = await Promise.all([fetchJson('/api/version'), fetchJson('/api/build')]);
    const sha = String(build.commit_sha || '').slice(0, 12);
    if (el) el.textContent = `${version.application || 'Hidden Conviction'} v${version.version || ''}${sha ? ` · build ${sha}` : ''}`.trim();
  } catch (_) {
    if (el) el.textContent = 'Hidden Conviction';
  }
}

async function loadSystemStatus() {
  const el = document.getElementById('system-status');
  if (!el) return;
  const delayBadge = document.getElementById('data-delay-badge');
  try {
    const data = await fetchJson('/api/health');
    const queue = data.checks?.queue || {};
    const cron = data.checks?.cron || {};
    const backlog = Number(queue.pending || 0);
    const errors = Number(queue.errors || 0);
    const oldestMinutes = Number(queue.oldest_pending_age_minutes || 0);
    const reasons = [];
    if (backlog) reasons.push(`filing backlog ${compactNumber(backlog)}`);
    if (errors) reasons.push(`${compactNumber(errors)} error${errors === 1 ? '' : 's'}`);
    if (!cron.last_success_at) reasons.push('no successful cron receipt yet');
    const successAge = relativeTime(cron.last_success_at);
    const successText = successAge ? ` · last successful update ${successAge}` : '';
    const state = data.operational_state || (data.status === 'ok' ? 'operational' : 'degraded');
    el.className = `system-status ${state === 'operational' ? 'ok' : 'warn'}`;
    const label = { operational: 'System operational', delayed: 'System delayed', degraded: 'System degraded', failed: 'System failed' }[state] || 'System status';
    el.textContent = state === 'operational'
      ? `${label}${backlog ? ` · ${compactNumber(backlog)} filing${backlog === 1 ? '' : 's'} processing` : ''}${successText}`
      : `${label}${reasons.length ? `: ${reasons.join(' · ')}` : ''}${successText}`;
    if (delayBadge) {
      const delayed = oldestMinutes >= 30 || backlog >= 500;
      delayBadge.hidden = !delayed;
      if (delayed) delayBadge.textContent = `Data delayed · oldest queued filing ${oldestMinutes >= 60 ? `${(oldestMinutes/60).toFixed(1)}h` : `${Math.round(oldestMinutes)}m`}`;
    }
  } catch (_) {
    el.className = 'system-status warn';
    el.textContent = 'System status unavailable';
    if (delayBadge) delayBadge.hidden = true;
  }
}

function currentFilters() {
  return {
    family: document.getElementById('filter-family')?.value || '',
    industry: document.getElementById('filter-industry')?.value.trim() || '',
    min_hcs: document.getElementById('filter-min-hcs')?.value || '0',
    sort: document.getElementById('filter-sort')?.value || 'score',
  };
}

function filterQuery(extra = {}) {
  const values = { ...currentFilters(), ...extra };
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== '' && value != null) params.set(key, value);
  }
  return params.toString();
}

function applyFiltersFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const family = document.getElementById('filter-family');
  const industry = document.getElementById('filter-industry');
  const minHcs = document.getElementById('filter-min-hcs');
  const sort = document.getElementById('filter-sort');
  if (family) family.value = params.get('family') || '';
  if (industry) industry.value = params.get('industry') || '';
  if (minHcs) minHcs.value = params.get('min_hcs') || '0';
  if (sort) sort.value = params.get('sort') || 'score';
}

function syncFiltersToUrl({ replace = false } = {}) {
  if (window.location.pathname !== '/') return;
  const filters = currentFilters();
  const params = new URLSearchParams();
  if (filters.family) params.set('family', filters.family);
  if (filters.industry) params.set('industry', filters.industry);
  if (Number(filters.min_hcs || 0) > 0) params.set('min_hcs', filters.min_hcs);
  if (filters.sort && filters.sort !== 'score') params.set('sort', filters.sort);
  const next = `${window.location.pathname}${params.toString() ? `?${params}` : ''}`;
  const method = replace ? 'replaceState' : 'pushState';
  window.history[method]({ filters: true }, '', next);
}

function setupFilters() {
  const family = document.getElementById('filter-family');
  const industry = document.getElementById('filter-industry');
  const minHcs = document.getElementById('filter-min-hcs');
  const sort = document.getElementById('filter-sort');
  const reset = document.getElementById('filter-reset');
  if (!family || !industry || !minHcs || !sort || !reset) return;
  let timer = null;
  const reload = ({ replace = false } = {}) => {
    syncFiltersToUrl({ replace });
    return Promise.allSettled([loadSignals(), loadCandidates()]);
  };
  family.addEventListener('change', () => reload());
  sort.addEventListener('change', () => reload());
  minHcs.addEventListener('change', () => reload());
  industry.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => reload({ replace: true }), 250);
  });
  reset.addEventListener('click', () => {
    family.value = '';
    industry.value = '';
    minHcs.value = '0';
    sort.value = 'score';
    reload();
  });
}

function setupCompanySearch() {
  const input = document.getElementById('company-search');
  const results = document.getElementById('search-results');
  if (!input || !results) return;

  let timer = null;
  let requestToken = 0;
  let activeIndex = -1;

  const options = () => [...results.querySelectorAll('[role="option"]')];
  const setActive = index => {
    const rows = options();
    if (!rows.length) { activeIndex = -1; input.removeAttribute('aria-activedescendant'); return; }
    activeIndex = ((index % rows.length) + rows.length) % rows.length;
    rows.forEach((row, i) => row.classList.toggle('active', i === activeIndex));
    input.setAttribute('aria-activedescendant', rows[activeIndex].id);
    rows[activeIndex].scrollIntoView({ block: 'nearest' });
  };
  const hide = () => {
    results.hidden = true;
    results.innerHTML = '';
    activeIndex = -1;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  };
  const run = async () => {
    const q = input.value.trim();
    if (!q) { hide(); return; }
    const token = ++requestToken;
    try {
      const payload = await fetchJson(`/api/search?q=${encodeURIComponent(q)}&limit=8`);
      if (token !== requestToken) return;
      const rows = payload.results || [];
      if (!rows.length) {
        results.innerHTML = '<div class="empty compact">No matching issuer.</div>';
        results.hidden = false;
        input.setAttribute('aria-expanded', 'true');
        return;
      }
      results.innerHTML = rows.map((row, index) => {
        const id = escapeHtml(displayIdentifier(row));
        const route = encodeURIComponent(routeIdentifier(row));
        const score = row.hcs_score == null ? 'No score' : `HCS ${Math.round(scoreValue(row))}`;
        return `<a id="search-option-${index}" class="search-result" role="option" aria-selected="false" href="/company/${route}"><div><span class="ticker">${id}</span><span class="name">${escapeHtml(row.company_name || '')}</span></div><span class="search-score">${escapeHtml(score)}</span></a>`;
      }).join('');
      results.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      activeIndex = -1;
    } catch (_) {
      if (token === requestToken) hide();
    }
  };

  input.setAttribute('aria-expanded', 'false');
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 180); });
  input.addEventListener('keydown', event => {
    if (event.key === 'Escape') { hide(); input.blur(); return; }
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive(activeIndex + 1); return; }
    if (event.key === 'ArrowUp') { event.preventDefault(); setActive(activeIndex - 1); return; }
    if (event.key === 'Enter') {
      const rows = options();
      const target = activeIndex >= 0 ? rows[activeIndex] : rows[0];
      if (target && !results.hidden) { event.preventDefault(); target.click(); }
    }
  });
  results.addEventListener('mousemove', event => {
    const row = event.target.closest('[role="option"]');
    if (!row) return;
    const index = options().indexOf(row);
    if (index >= 0) setActive(index);
  });
  document.addEventListener('click', event => { if (!event.target.closest('.research-search')) hide(); });
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault(); input.focus(); input.select();
    }
  });
}

async function loadFreshness() {
  try {
    const data = await fetchJson('/api/freshness');
    freshnessSnapshot = data;
    const through = prettyDate(data.latest_filing_at);
    const scoreDate = prettyDate(data.latest_score_date);
    const stale = Number(data.stale_active_scores || 0);
    const text = through
      ? `SEC filing coverage through ${through}${scoreDate ? ` · Scores through ${scoreDate}` : ''}${stale ? ` · ${stale} active score${stale === 1 ? '' : 's'} awaiting refresh` : ''}`
      : 'SEC filing coverage status unavailable';
    const home = document.getElementById('home-freshness');
    const footer = document.getElementById('footer-freshness');
    if (home) home.textContent = text;
    if (footer) {
      const refreshed = prettyTimestamp(data.latest_discovered_at);
      const relative = relativeTime(data.latest_discovered_at);
      footer.textContent = `${refreshed ? `Updated ${refreshed}${relative ? ` (${relative})` : ''} · ` : ''}${text}`;
    }

    const pipeline = document.getElementById('pipeline-status');
    if (pipeline) {
      const labels = { form4: 'Form 4', ownership: 'Schedule 13', '8k': '8-K', '13f': '13F' };
      pipeline.innerHTML = Object.entries(labels).map(([key, label]) => {
        const row = data.pipelines?.[key] || {};
        const date = prettyDate(row.latest_filing_at);
        const backlog = Number(row.unprocessed || 0);
        const failed = Number(row.discovery?.consecutive_failures || 0);
        const warn = backlog > 0 || failed > 0;
        return `<span class="pipeline-pill ${warn ? 'warn' : ''}"><strong>${label}</strong> ${date || 'no data'}${backlog ? ` · ${compactNumber(backlog)} pending` : ''}${failed ? ` · ${compactNumber(failed)} fetch failure${failed === 1 ? '' : 's'}` : ''}</span>`;
      }).join('');
    }
  } catch (_) {
    const home = document.getElementById('home-freshness');
    if (home) home.textContent = 'Data freshness temporarily unavailable';
  }
}

async function loadSignals() {
  const status = document.getElementById('status');
  const container = document.getElementById('signals');
  if (!status || !container) return;
  container.innerHTML = '<div class="skeleton-card"></div><div class="skeleton-card"></div>';
  try {
    const selectedMin = Math.max(80, Number(document.getElementById('filter-min-hcs')?.value || 0));
    const query = filterQuery({ limit: 20, min_hcs: selectedMin });
    const payload = await fetchJson(`/api/signals?${query}`);
    const rows = payload.results || [];
    const total = Number(payload.meta?.total ?? rows.length);
    status.textContent = `${total} live signal${total === 1 ? '' : 's'}`;
    if (!rows.length) {
      const top = freshnessSnapshot?.highest_current_score || {};
      const topText = top.hcs_score != null
        ? `<div class="highest-score-note">Current highest eligible HCS: <strong>${Math.round(Number(top.hcs_score))}</strong>${top.ticker ? ` · <a href="/company/${encodeURIComponent(top.ticker)}">${escapeHtml(top.ticker)}</a>` : ''}</div>`
        : '';
      container.innerHTML = `<div class="empty"><strong>No companies currently meet the HCS ≥ 80 threshold.</strong><span>The publication threshold remains strict even when the list is empty.</span>${topText}</div>`;
      return;
    }
    container.innerHTML = rows.map(row => renderCard(row)).join('');
  } catch (_) {
    status.textContent = 'Unavailable';
    container.innerHTML = '<div class="empty"><strong>Signal data is temporarily unavailable.</strong><button class="retry-button" data-retry="signals" type="button">Retry</button></div>';
  }
}

async function loadCandidates() {
  const status = document.getElementById('candidate-status');
  const container = document.getElementById('candidates');
  if (!status || !container) return;
  container.innerHTML = '<div class="skeleton-card"></div><div class="skeleton-card"></div>';
  try {
    const selectedMin = Math.max(0.01, Number(document.getElementById('filter-min-hcs')?.value || 0));
    const query = filterQuery({ limit: 10, min_hcs: selectedMin });
    const payload = await fetchJson(`/api/candidates?${query}`);
    const rows = payload.results || [];
    status.textContent = `${rows.length} current candidate${rows.length === 1 ? '' : 's'}`;
    if (!rows.length) {
      container.innerHTML = '<div class="empty">No below-threshold activity candidates match the current filters.</div>';
      return;
    }
    container.innerHTML = rows.map(row => renderCard(row, { showMeter: true })).join('');
  } catch (_) {
    status.textContent = 'Unavailable';
    container.innerHTML = '<div class="empty"><strong>Current activity data is temporarily unavailable.</strong><button class="retry-button" data-retry="candidates" type="button">Retry</button></div>';
  }
}

function evidenceDescription(item) {
  if (item.family === 'Insider Buying' || item.family === 'Insider Cluster') {
    const who = [item.insider_name, item.insider_role].filter(Boolean).join(' · ');
    const parts = [];
    if (who) parts.push(who);
    if (item.shares != null) parts.push(`${compactNumber(item.shares)} shares`);
    if (item.transaction_value != null) parts.push(money(item.transaction_value));
    return parts.join(' · ') || 'Reported insider purchase';
  }
  if (item.family === 'Beneficial Ownership') {
    const parts = [item.investor_name, item.schedule_type].filter(Boolean);
    if (item.ownership_pct != null) parts.push(`${percent(item.ownership_pct)} ownership`);
    if (item.ownership_change_pp != null) parts.push(`${Number(item.ownership_change_pp) >= 0 ? '+' : ''}${percent(item.ownership_change_pp)} pts`);
    return parts.join(' · ') || 'Beneficial ownership filing';
  }
  if (item.family === 'Institutional Accumulation') {
    if (item.scoring_reason) return item.scoring_reason;
    const parts = [item.manager_name].filter(Boolean);
    if (item.share_change_pct != null) parts.push(`${Number(item.share_change_pct) >= 0 ? '+' : ''}${percent(item.share_change_pct)} shares`);
    if (item.position_weight_pct != null) parts.push(`${percent(item.position_weight_pct)} of reported portfolio`);
    return parts.join(' · ') || 'Institutional holdings confirmation';
  }
  if (item.family === 'Corporate Event') {
    const itemLabel = item.primary_item ? `Item ${item.primary_item}` : (item.item_numbers ? `Item ${item.item_numbers}` : '');
    const headline = [item.event_type, itemLabel, item.sentiment].filter(Boolean).join(' · ') || '8-K event';
    return item.scoring_reason ? `${headline}. ${item.scoring_reason}` : headline;
  }
  return item.family || 'SEC filing';
}

function renderHistorySvg(history) {
  const rows = [...(history || [])].reverse().slice(-30);
  if (!rows.length) return '<div class="history-empty">No score history yet.</div>';
  const width = 720;
  const height = 220;
  const padX = 28;
  const padY = 22;
  const innerW = width - padX * 2;
  const innerH = height - padY * 2;
  const points = rows.map((item, index) => {
    const score = Math.max(0, Math.min(100, Number(item.hcs_score || 0)));
    const x = rows.length === 1 ? width / 2 : padX + (index / (rows.length - 1)) * innerW;
    const y = padY + ((100 - score) / 100) * innerH;
    return { x, y, score, date: String(item.score_date || '') };
  });
  const pointString = points.map(point => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ');
  const labels = points.filter((_, index) => index === 0 || index === points.length - 1 || index % Math.max(1, Math.floor(points.length / 5)) === 0);
  return `
    <div class="history-chart-wrap">
      <svg class="history-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Recent Hidden Conviction Score history">
        <line class="history-threshold" x1="${padX}" x2="${width - padX}" y1="${(padY + .2 * innerH).toFixed(1)}" y2="${(padY + .2 * innerH).toFixed(1)}"></line>
        <text class="history-threshold-label" x="${width - padX}" y="${(padY + .2 * innerH - 6).toFixed(1)}" text-anchor="end">HCS 80</text>
        <polyline class="history-line" points="${pointString}" fill="none"></polyline>
        ${points.map(point => `<circle class="history-point" cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="4"><title>${escapeHtml(prettyDate(point.date))}: HCS ${Math.round(point.score)}</title></circle>`).join('')}
        ${labels.map(point => `<text class="history-date" x="${point.x.toFixed(1)}" y="${height - 5}" text-anchor="middle">${escapeHtml(point.date.slice(5))}</text>`).join('')}
      </svg>
    </div>`;
}

function renderCompanyPage(payload) {
  const company = payload.company || {};
  const score = payload.score || {};
  const signals = payload.signals || [];
  const evidence = payload.evidence || [];
  const freshness = payload.freshness || {};
  const institutional = payload.institutional_confirmation || {};
  const history = payload.score_history || [];
  const changes = payload.score_changes || {};
  const timeline = payload.timeline || [];
  const whyNow = payload.why_now || '';
  const whyChanged = payload.why_changed || '';
  const identifier = displayIdentifier(company);
  const hcs = scoreValue(score);
  const eligible = company.hcs_eligible !== false;
  const displayedHcs = eligible ? Math.round(hcs) : '—';
  const displayedClassification = eligible ? (score.classification || 'No Signal') : 'Outside Public Universe';
  const universeNote = eligible ? '' : `<div class="freshness company-freshness"><strong>Universe filter:</strong> ${escapeHtml(company.hcs_exclusion_reason || 'This issuer is not included in the public operating-company HCS ranking.')}</div>`;
  const institutionalNote = institutional.data_through
    ? `<div class="freshness company-freshness"><strong>Institutional data:</strong> through ${escapeHtml(prettyDate(institutional.data_through))}${institutional.latest_filed_at ? ` · latest filing ${escapeHtml(prettyDate(institutional.latest_filed_at))}` : ''}. 13F holdings are delayed and are used only as confirmation of fresher signals.</div>`
    : '';
  const staleNote = score.score_date && score.is_current === false
    ? `<div class="stale-warning"><strong>Historical score.</strong> This issuer has not yet been materialized for the current UTC date, so this score is excluded from current rankings.</div>`
    : '';
  const changeHtml = Object.entries({ '1d': '1 day', '7d': '7 days', '30d': '30 days' }).map(([key, label]) => {
    const value = changes[key];
    if (!value) return `<div class="change-chip"><span>${label}</span><strong>—</strong></div>`;
    const n = Number(value.change || 0);
    return `<div class="change-chip"><span>${label}</span><strong>${n > 0 ? '+' : ''}${n.toFixed(1)}</strong></div>`;
  }).join('');
  const historyHtml = renderHistorySvg(history);
  const todayDelta = Number(score.score_delta || 0);
  const todayDeltaText = Number.isFinite(todayDelta) && todayDelta !== 0
    ? `${todayDelta > 0 ? '+' : ''}${todayDelta.toFixed(1)} today`
    : 'unchanged today';

  document.title = `${company.ticker || company.company_name || company.cik} · Hidden Conviction`;

  const signalHtml = signals.length
    ? signals.map(signal => `
        <div class="signal-chip ${signal.direction === 'negative' ? 'negative' : ''}">
          <strong>${escapeHtml(signal.label)}</strong>
          <span>${escapeHtml(signal.description)}</span>
        </div>
      `).join('')
    : '<div class="empty compact">No active signal family is currently contributing to HCS.</div>';

  const evidenceHtml = evidence.length
    ? evidence.map(item => `
        <article class="evidence-row">
          <div>
            <div class="evidence-family">${escapeHtml(item.family || item.form_type || 'SEC filing')}</div>
            <div class="evidence-desc">${escapeHtml(evidenceDescription(item))}</div>
            <div class="muted small">${escapeHtml(prettyDate(item.event_date || item.filed_at))}${item.form_type ? ` · Form ${escapeHtml(item.form_type)}` : ''}${item.accepted_at ? ` · Accepted ${escapeHtml(prettyTimestamp(item.accepted_at))}` : ''}</div>
          </div>
          ${safeSecUrl(item.filing_url) ? `<div class="source-actions"><a class="source-link" href="${escapeHtml(safeSecUrl(item.filing_url))}" target="_blank" rel="noopener noreferrer">View SEC filing ↗</a><button class="copy-source" type="button" data-copy-url="${escapeHtml(safeSecUrl(item.filing_url))}">Copy link</button></div>` : ''}
        </article>
      `).join('')
    : '<div class="empty compact">No source filing is currently attached to an active signal family.</div>';

  const latest = prettyDate(freshness.latest_relevant_filing_at);
  const scored = prettyDate(freshness.score_date);

  return `
    <nav class="breadcrumb" aria-label="Breadcrumb"><a href="/">Signals</a><span>›</span><span>${escapeHtml(company.ticker || company.cik || identifier)}</span><span>›</span><span>${escapeHtml(company.company_name || identifier)}</span></nav>
    <header class="company-hero">
      <div class="company-heading">
        <div class="eyebrow">${escapeHtml(identifier)}</div>
        <h1 class="page-title">${escapeHtml(company.company_name || identifier)}</h1>
        <div class="company-meta">${company.exchange ? `${escapeHtml(company.exchange)} · ` : ''}CIK ${escapeHtml(company.cik || '')}${company.industry ? ` · ${escapeHtml(company.industry)}` : ''}${company.sic ? ` · SIC ${escapeHtml(company.sic)}` : ''}</div>
      </div>
      <div class="hero-score">
        <div class="score giant">${displayedHcs}</div>
        <div class="score-label" title="HCS means Hidden Conviction Score">HCS</div>
        <div class="classification">${escapeHtml(displayedClassification)}</div>
        <div class="score-delta">${escapeHtml(todayDeltaText)}</div>
      </div>
    </header>

    <div class="freshness company-freshness">${latest ? `Latest relevant filing ${latest}` : 'No active source filing date'}${scored ? ` · Score dated ${scored}` : ''}</div>
    ${universeNote}
    ${institutionalNote}
    ${staleNote}
    ${whyNow ? `<div class="why-now"><strong>Why now?</strong> ${escapeHtml(whyNow)}</div>` : ''}
    ${whyChanged ? `<div class="why-changed"><strong>Why did the score change?</strong> ${escapeHtml(whyChanged)}</div>` : ''}
    <div class="change-grid" aria-label="HCS change">${changeHtml}</div>

    <section class="detail-section">
      <div class="section-head detail-head"><div><h2>Why this score?</h2><p class="section-copy">Active signal families are shown without exposing proprietary component weights.</p></div></div>
      <div class="signal-chip-grid">${signalHtml}</div>
    </section>

    <section class="detail-section">
      <div class="section-head detail-head"><div><h2>Source evidence</h2><p class="section-copy">The underlying public SEC filings supporting the currently active signal families.</p></div></div>
      <div class="evidence-list">${evidenceHtml}</div>
    </section>

    <section class="detail-section">
      <div class="section-head detail-head"><div><h2>Signal timeline</h2><p class="section-copy">The active evidence events behind the current public signal.</p></div></div>
      <div class="timeline">${timeline.length ? timeline.map(item => `<div class="timeline-item"><strong>${escapeHtml(item.family || item.form_type || 'SEC filing')}</strong><span>${escapeHtml(prettyDate(item.event_date || item.filed_at))}${item.form_type ? ` · Form ${escapeHtml(item.form_type)}` : ''}</span></div>`).join('') : '<div class="history-empty">No active evidence timeline.</div>'}</div>
    </section>

    <section class="detail-section">
      <div class="section-head detail-head"><div><h2>Score history</h2><p class="section-copy">Up to 30 recent materialized daily scores. Historical rows are preserved rather than rewritten.</p></div></div>
      <div class="history-visual" aria-label="Recent HCS history">${historyHtml}</div>
    </section>

    <section class="detail-section mini-method">
      <strong>How to interpret HCS</strong>
      <p>HCS summarizes filing-driven conviction signals. A higher score indicates stronger convergence in the configured signal framework, not a guarantee of future performance.</p>
      <a href="/methodology">Read the methodology →</a>
    </section>
  `;
}

async function loadCompany(identifier) {
  const container = document.getElementById('company-content');
  try {
    const payload = await fetchJson(`/api/company/${encodeURIComponent(identifier)}`);
    container.innerHTML = renderCompanyPage(payload);
  } catch (error) {
    document.title = 'Company not found · Hidden Conviction';
    container.innerHTML = `
      <div class="not-found">
        <div class="eyebrow">NOT FOUND</div>
        <h1 class="page-title">Company not found.</h1>
        <p class="lede">We could not find a company matching ${escapeHtml(identifier)}.</p>
        <a class="back-link" href="/">Return to current signals</a>
        <button class="retry-button" data-retry="company" data-identifier="${escapeHtml(identifier)}" type="button">Retry</button>
      </div>`;
  }
}

function stageLabel(key) {
  const labels = { database: 'Database', sec: 'SEC Access', discovery: 'Discovery', queue: 'Queue', processing: 'Processing', scoring: 'Scoring', public_api: 'Public API', query_performance: 'Query Performance', cron: 'Cron' };
  return labels[key] || key;
}

function stageDetail(key, stage) {
  if (key === 'sec') return `${compactNumber(stage.failed_today || 0)} failures today${stage.latest_latency_seconds != null ? ` · latest latency ${Number(stage.latest_latency_seconds).toFixed(1)}s` : ''}`;
  if (key === 'discovery') return `${stage.reconciled_through ? `Reconciled through ${prettyDate(stage.reconciled_through)}` : 'Reconciliation pending'}${stage.needs_backfill ? ` · ${stage.needs_backfill} backfill flag${stage.needs_backfill === 1 ? '' : 's'}` : ''}`;
  if (key === 'queue') return `${compactNumber(stage.pending || 0)} pending · ${compactNumber(stage.processing || 0)} processing · ${compactNumber(stage.errors || 0)} errors${stage.oldest_pending_age_minutes != null ? ` · oldest ${Number(stage.oldest_pending_age_minutes).toFixed(1)}m` : ''}`;
  if (key === 'processing') return stage.latest_processed_at ? `Last processed ${prettyTimestamp(stage.latest_processed_at)}` : 'No processed receipt yet';
  if (key === 'scoring') return `${stage.latest_score_date ? `Scores through ${prettyDate(stage.latest_score_date)}` : 'No score date'} · ${compactNumber(stage.stale_active_scores || 0)} stale${stage.minutes_since_latest_score != null ? ` · latest ${Number(stage.minutes_since_latest_score).toFixed(1)}m ago` : ''}`;
  if (key === 'cron') return stage.last_success_at ? `Last success ${prettyTimestamp(stage.last_success_at)}${stage.last_duration_ms != null ? ` · ${stage.last_duration_ms} ms` : ''} · ${Number(stage.records_discovered || 0)} discovered · ${Number(stage.records_processed || 0)} processed` : 'No successful cron receipt yet';
  if (key === 'query_performance') return `${compactNumber(stage.executions_today || 0)} queries · ${compactNumber(stage.slow_queries_today || 0)} slow · max ${Number(stage.max_duration_ms || 0).toFixed(1)} ms`;
  if (key === 'database') return 'D1 reachable';
  if (key === 'public_api') return 'Public research endpoints available';
  return '';
}

async function loadSystemConsole() {
  const overall = document.getElementById('system-overall');
  const container = document.getElementById('system-stages');
  if (!overall || !container) return;
  try {
    const payload = await fetchJson('/api/system');
    overall.className = `system-overall ${payload.status || 'unknown'}`;
    overall.textContent = `Overall status: ${(payload.status || 'unknown').toUpperCase()}`;
    container.innerHTML = Object.entries(payload.stages || {}).map(([key, stage]) => {
      const details = Object.entries(stage || {})
        .filter(([field, value]) => field !== 'status' && value != null && value !== '')
        .map(([field, value]) => `<div><span>${escapeHtml(field.replaceAll('_', ' '))}</span><strong>${escapeHtml(String(value))}</strong></div>`).join('');
      return `
      <details class="system-stage ${escapeHtml(stage.status || 'unknown')}">
        <summary><div><strong>${escapeHtml(stageLabel(key))}</strong><span>${escapeHtml(stageDetail(key, stage || {}))}</span></div><span class="stage-state">${escapeHtml(String(stage.status || 'unknown').toUpperCase())}</span></summary>
        <div class="stage-drilldown">${details || '<div>No additional public detail.</div>'}</div>
      </details>`;
    }).join('');
  } catch (_) {
    overall.textContent = 'System state unavailable';
    container.innerHTML = '<div class="empty">Unable to load system console.</div>';
  }
}

function setupSystemRefresh() {
  const button = document.getElementById('system-refresh');
  const note = document.getElementById('system-refresh-note');
  if (!button) return;
  button.addEventListener('click', () => {
    button.disabled = true;
    if (note) note.textContent = 'Refreshing…';
    window.location.reload();
  });
}

async function loadWebChecks() {
  const el = document.getElementById('system-webchecks');
  if (!el) return;
  const check = async path => {
    try { const r = await fetch(path, { cache: 'no-store' }); return r.ok; } catch (_) { return false; }
  };
  const [robots, sitemap] = await Promise.all([check('/robots.txt'), check('/sitemap.xml')]);
  el.innerHTML = `<a href="/robots.txt">robots.txt</a> ${robots ? '✓' : '⚠'} · <a href="/sitemap.xml">sitemap.xml</a> ${sitemap ? '✓' : '⚠'}`;
}

function setupAdminDiagnostics() {
  const button = document.getElementById('admin-load');
  const keyInput = document.getElementById('admin-key');
  const output = document.getElementById('admin-diagnostics');
  if (!button || !keyInput || !output) return;
  button.addEventListener('click', async () => {
    const key = keyInput.value.trim();
    if (!key) { output.textContent = 'Enter the admin key.'; return; }
    output.innerHTML = '<div class="empty compact">Loading diagnostics…</div>';
    try {
      const response = await fetch('/api/admin/diagnostics', { headers: { Accept: 'application/json', 'X-Admin-Key': key } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const failures = data.processing_errors || [];
      const scores = data.score_recompute_errors || [];
      output.innerHTML = `
        <div class="admin-summary">${failures.length} filing failures · ${scores.length} score failures · ${(data.open_data_quality_issues || []).length} open data-quality issues ${failures.length ? '<button type="button" id="admin-replay-all">Reprocess all filing failures</button>' : ''}</div>
        <div class="admin-failures">${failures.map(item => `<div class="admin-row"><span>#${escapeHtml(item.id)} · ${escapeHtml(item.form_type || '')} · ${escapeHtml(item.accession_number || '')}<small>${escapeHtml(item.last_error || '')}</small></span><button type="button" data-admin-replay-filing="${escapeHtml(item.id)}">Replay</button></div>`).join('') || '<div class="empty compact">No filing failures.</div>'}</div>
        <div class="admin-failures">${scores.map(item => `<div class="admin-row"><span>CIK ${escapeHtml(item.cik || '')}<small>${escapeHtml(item.last_error || '')}</small></span><button type="button" data-admin-replay-score="${escapeHtml(item.cik)}">Replay score</button></div>`).join('')}</div>`;
      output.querySelectorAll('[data-admin-replay-filing]').forEach(el => el.addEventListener('click', () => replayAdmin(`/api/admin/replay/filing/${encodeURIComponent(el.dataset.adminReplayFiling)}`, key, el)));
      output.querySelectorAll('[data-admin-replay-score]').forEach(el => el.addEventListener('click', () => replayAdmin(`/api/admin/replay/score/${encodeURIComponent(el.dataset.adminReplayScore)}`, key, el)));
      const replayAll = output.querySelector('#admin-replay-all');
      if (replayAll) replayAll.addEventListener('click', () => replayAdmin('/api/admin/replay/failures', key, replayAll));
    } catch (error) {
      output.textContent = error.status === 401 ? 'Admin key rejected.' : 'Diagnostics unavailable.';
    }
  });
}

async function replayAdmin(url, key, button) {
  button.disabled = true;
  try {
    const response = await fetch(url, { method: 'POST', headers: { Accept: 'application/json', 'X-Admin-Key': key } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    button.textContent = 'Queued';
  } catch (_) {
    button.textContent = 'Failed';
    button.disabled = false;
  }
}

async function initPage() {
  const page = document.body.dataset.page || 'home';
  const ssr = document.body.dataset.ssrRendered === 'true';
  if (!ssr) await loadFreshness();

  if (page === 'home') {
    applyFiltersFromUrl();
    setupCompanySearch();
    setupFilters();
    window.addEventListener('popstate', () => {
      applyFiltersFromUrl();
      Promise.allSettled([loadSignals(), loadCandidates()]);
    });
    if (ssr) {
      setTimeout(() => Promise.allSettled([loadFreshness(), loadSystemStatus()]), 0);
    } else {
      await Promise.allSettled([loadSignals(), loadCandidates(), loadSystemStatus()]);
    }
    return;
  }

  if (page === 'company') {
    const match = window.location.pathname.match(/^\/company\/(.+)$/);
    if (!ssr && match) await loadCompany(decodeURIComponent(match[1]));
    return;
  }

  if (page === 'system') {
    setupAdminDiagnostics();
    setupSystemRefresh();
    setTimeout(() => loadWebChecks(), 0);
    if (!ssr) await loadSystemConsole();
  }
}

document.addEventListener('click', async event => {
  const copy = event.target.closest('[data-copy-url]');
  if (copy) {
    try {
      await navigator.clipboard.writeText(copy.dataset.copyUrl || '');
      const old = copy.textContent; copy.textContent = 'Copied';
      setTimeout(() => { copy.textContent = old; }, 1200);
    } catch (_) { copy.textContent = 'Copy failed'; }
    return;
  }
  const button = event.target.closest('[data-retry]');
  if (!button) return;
  const target = button.dataset.retry;
  if (target === 'signals') loadSignals();
  if (target === 'candidates') loadCandidates();
  if (target === 'company') loadCompany(button.dataset.identifier || '');
});

if (document.body.dataset.ssrRendered !== 'true') Promise.allSettled([loadAppVersion()]);
initPage();
