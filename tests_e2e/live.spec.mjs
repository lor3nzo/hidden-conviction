import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const disclaimer = 'The information provided on this website is for informational and educational purposes only';

test('v0.14 route documents are distinct and server rendered at origin', async ({ request }) => {
  const checks = [
    ['/', 'data-page="home"', ['view-company', 'view-system', 'view-methodology']],
    ['/system', 'data-page="system"', ['view-home', 'view-company', 'view-methodology']],
    ['/methodology', 'data-page="methodology"', ['view-home', 'view-company', 'view-system']],
  ];
  for (const [path, marker, forbidden] of checks) {
    const response = await request.get(path, { headers: { 'Cache-Control': 'no-cache' } });
    expect(response.ok()).toBeTruthy();
    const html = await response.text();
    expect(html).toContain(marker);
    expect(html).toContain(disclaimer);
    for (const token of forbidden) expect(html).not.toContain(token);
  }
});

test('public edge exposes build identity and lightweight architecture', async ({ request }) => {
  const response = await request.get('/', { headers: { 'Cache-Control': 'no-cache' } });
  expect(response.headers()['x-edge-architecture']).toBe('public-js-v1');
  expect(response.headers()['x-hidden-conviction-version']).toBe('0.14.0');
  expect(response.headers()['x-hidden-conviction-build']).toBeTruthy();
});

test('system console is server rendered with trends and operational history', async ({ request }) => {
  const system = await request.get('/system', { headers: { 'Cache-Control': 'no-cache' } });
  expect(system.ok()).toBeTruthy();
  const html = await system.text();
  expect(html).toContain('data-ssr-rendered="true"');
  expect(html).toContain('7 day pipeline activity');
  expect(html).toContain('Query performance today');
  expect(html).toContain('Recent cron runs');
  expect(html).toContain('Estimated clearance');
  expect(html).toContain('24 hour backlog trend');
  expect(html).toContain('Deployment comparison');
  expect(html).not.toContain('Loading system state');
});

test('company route returns route-specific research and disclaimer', async ({ request }) => {
  const candidatesResponse = await request.get('/api/candidates?limit=1');
  expect(candidatesResponse.ok()).toBeTruthy();
  const payload = await candidatesResponse.json();
  test.skip(!(payload.results || []).length, 'No current company candidate is available');
  const row = payload.results[0];
  const identifier = row.ticker || row.cik;
  const response = await request.get(`/company/${encodeURIComponent(identifier)}`);
  expect(response.ok()).toBeTruthy();
  const html = await response.text();
  expect(html).toContain('data-page="company"');
  expect(html).toContain('data-ssr-rendered="true"');
  expect(html).toContain('Source evidence');
  expect(html).toContain('Score history');
  expect(html).toContain(disclaimer);
  expect(html).not.toContain('view-home');
});

test('critical pages have no serious or critical accessibility violations', async ({ page }) => {
  for (const path of ['/', '/system', '/methodology']) {
    await page.goto(path, { waitUntil: 'networkidle' });
    const results = await new AxeBuilder({ page }).analyze();
    const severe = results.violations.filter(v => ['serious', 'critical'].includes(v.impact));
    expect(severe, `${path}: ${severe.map(v => v.id).join(', ')}`).toEqual([]);
  }
});

test('capture v0.14 visual regression reference screenshots', async ({ page }) => {
  await page.goto('/', { waitUntil: 'networkidle' });
  await page.screenshot({ path: 'test-results/v014-home.png', fullPage: true });
  await page.goto('/system', { waitUntil: 'networkidle' });
  await page.screenshot({ path: 'test-results/v014-system.png', fullPage: true });
});
