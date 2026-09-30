const { test, expect } = require('./session.cjs');

test('only the launch URL opens the UI, and its token leaves the address', async ({ browser }, testInfo) => {
  const baseURL = testInfo.project.use.baseURL;
  const context = await browser.newContext({ baseURL });
  try {
    const page = await context.newPage();
    const refused = await page.goto('/applications');
    expect(refused.status()).toBe(401);
    await expect(page.getByRole('heading', { name: 'Open Piceli from its launch address' })).toBeVisible();
    expect(await context.cookies()).toEqual([]);
    await page.goto(`/applications?token=${encodeURIComponent(process.env.PICELI_UI_LAUNCH_TOKEN)}`);
    await expect(page).toHaveURL(new URL('/applications', baseURL).href);
    await expect(page.getByRole('heading', { name: 'Applications', exact: true })).toBeVisible();
  } finally {
    await context.close();
  }
});

test('external link can open the loopback UI without granting cross-site API access', async ({ page }) => {
  const target = new URL('/applications', test.info().project.use.baseURL).href;
  await page.goto(`data:text/html,${encodeURIComponent(`<a href="${target}">Open Piceli</a>`)}`);
  const navigation = page.waitForRequest(request => request.isNavigationRequest() && request.url() === target);
  await page.getByRole('link', { name: 'Open Piceli' }).click();
  const request = await navigation;
  expect(['cross-site', 'same-site']).toContain((await request.allHeaders())['sec-fetch-site']);
  await expect(page.getByRole('heading', { name: 'Applications', exact: true })).toBeVisible();
});

test('localhost page navigation redirects to the configured loopback origin', async ({ page }) => {
  const canonical = new URL('/applications', test.info().project.use.baseURL);
  const alias = new URL(canonical);
  alias.hostname = 'localhost';
  await page.goto(`data:text/html,${encodeURIComponent(`<a href="${alias.href}">Open Piceli</a>`)}`);
  await page.getByRole('link', { name: 'Open Piceli' }).click();
  await expect(page).toHaveURL(canonical.href);
  await expect(page.getByRole('heading', { name: 'Applications', exact: true })).toBeVisible();
});

test('inventory scope exposes actual resource identity without invented sync', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/applications');
  await expect(page.getByRole('heading', { name: 'Applications', exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Shop' }).click();
  await expect(page.getByRole('heading', { name: 'Shop', exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Resources', exact: true }).click();
  await expect(page.getByText('web', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('credential', { exact: true }).first()).toBeVisible();
  await expect(page.getByLabel('Application state')).toContainText('Connected');
  await expect(page.getByText('No comparison available', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /^Deploy$/ })).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('YnJvd3Nlci1wcml2YXRl');
  expect(errors).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('keyboard search survives reload and resource deep links', async ({ page }) => {
  await page.goto('/applications');
  const search = page.getByRole('searchbox', { name: 'Search applications' });
  await search.focus();
  await page.keyboard.type('Shop');
  await expect(page).toHaveURL(/q=Shop/);
  await page.reload();
  await expect(search).toHaveValue('Shop');
  const application = page.getByRole('link', { name: 'Shop' });
  await application.focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/applications\/shop\/overview$/);
  const resources = page.getByRole('link', { name: 'Resources', exact: true });
  await resources.focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/applications\/shop\/resources$/);
  await page.reload();
  await expect(page.getByText('web', { exact: true }).first()).toBeVisible();
  const inspect = page.getByRole('button', { name: 'Inspect Deployment web in piceli-test', exact: true });
  await inspect.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('dialog', { name: 'Resource details' })).toBeVisible();
  await expect(page).toHaveURL(/resource=/);
  await page.reload();
  await expect(page.getByRole('dialog', { name: 'Resource details' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: 'Resource details' })).toHaveCount(0);
});

test('transport failure is visible while previous resource data stays inspectable', async ({ page }) => {
  await page.goto('/applications/shop/resources');
  await expect(page.getByText('web', { exact: true }).first()).toBeVisible();
  await page.route('**/api/v1/applications/shop/resources*', route => route.abort('connectionfailed'));
  await page.getByRole('button', { name: 'Refresh resources', exact: true }).click();
  await expect(page.getByText('Observation unavailable', { exact: true }).first()).toBeVisible({ timeout: 15000 });
  await expect(page.getByText('web', { exact: true }).first()).toBeVisible();
  await expect(page.locator('body')).toContainText(/stale|connection|unavailable/i);
});

test('partial empty observation retains stale resources while a complete empty observation clears them', async ({ page }) => {
  await page.goto('/applications/shop/resources');
  const inspect = page.getByRole('button', { name: 'Inspect Deployment web in piceli-test', exact: true });
  await expect(inspect).toBeVisible();
  let partial = true;
  await page.route('**/api/v1/applications/shop/resources', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      items: [],
      cursor: partial ? 'failed-observation' : 'complete-observation',
      next_page: null,
      freshness: { state: partial ? 'unavailable' : 'connected', observed_at: new Date().toISOString(), reason: partial ? 'partial-observation' : null },
      partial: partial ? [{ scope: 'target', code: 'ui-observation-unavailable' }] : [],
    }),
  }));
  await page.getByRole('button', { name: 'Refresh resources', exact: true }).click();
  await expect(page.getByText('Partial observation', { exact: true })).toBeVisible();
  await expect(inspect).toBeVisible();
  await expect(page.locator('body')).toContainText(/stale|last observed/i);
  partial = false;
  await page.getByRole('button', { name: 'Refresh resources', exact: true }).click();
  await expect(inspect).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'No resources observed', exact: true })).toBeVisible();
});

test('Pipeline, Environments and GitOps controls fit this viewport and require keyboard review', async ({ page }) => {
  const first = { id: 'plan-1', digest: 'a'.repeat(64), phase: 'preliminary', materialized: false, target: { name: 'shop', namespace: 'piceli-test' }, expires_at: '2099-01-01T00:00:00Z', stages: [{ name: 'build', state: 'planned' }, { name: 'deliver', state: 'planned' }] };
  const second = { ...first, id: 'plan-2', digest: 'b'.repeat(64), phase: 'final', materialized: true, stages: [{ name: 'prerollout', state: 'planned', checks: [{ workload: 'web' }] }, { name: 'backup', state: 'planned', claims: [{ claim: 'data-web' }] }, { name: 'apply', state: 'planned' }] };
  const posted = [];
  const answer = (route, value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
  await page.route('**/api/v1/capabilities', async route => {
    const response = await route.fetch();
    const body = await response.json();
    for (const name of ['pipeline', 'environments', 'gitops', 'environment_change', 'gitops_change']) body.actions[name] = { allowed: true };
    await answer(route, body);
  });
  await page.route('**/api/v1/pipeline/**', route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === 'POST') posted.push(path);
    if (path.endsWith('/plans') && route.request().method() === 'POST') return answer(route, first);
    if (path.endsWith('/plans/plan-1')) return answer(route, first);
    if (path.endsWith('/plans/plan-2')) return answer(route, second);
    if (path.endsWith('/operations') && route.request().method() === 'POST') return answer(route, { id: 'run-1', state: 'queued' }, 202);
    if (path.endsWith('/operations/run-1/approve')) return answer(route, { id: 'run-1', state: 'queued' }, 202);
    if (path.endsWith('/operations/run-1')) return answer(route, { id: 'run-1', state: 'awaiting-review', next_plan_id: 'plan-2', plan_id: 'plan-1', stages: { build: 'done', deliver: 'done' }, created_at: '2026-09-30T00:00:00Z' });
    return answer(route, { items: [] });
  });
  await page.route('**/api/v1/environments', route => answer(route, { configured: true, items: [{ branch: 'wp-ui', namespace: 'piceli-test', main: false, state: 'running', health: 'healthy', commit: 'abcd', age_seconds: 120, workloads: [{ workload: 'web', ready: 1, replicas: 1 }], application_id: 'shop' }] }));
  await page.route('**/api/v1/gitops', route => answer(route, { configured: true, controller: { state: 'running' }, envs: [{ branch: 'wp-ui', namespace: 'piceli-test', state: 'approval-required', commit: 'abcd', plan_hash: 'c'.repeat(64) }] }));
  await page.goto('/applications');
  const nav = page.getByRole('navigation', { name: 'Main navigation' });
  await nav.getByRole('link', { name: 'Pipeline' }).focus();
  await page.keyboard.press('Enter');
  await page.getByRole('button', { name: 'Prepare new plan' }).click();
  const preliminary = page.getByRole('button', { name: 'Approve build and delivery' });
  await expect(preliminary).toBeDisabled();
  await page.getByRole('checkbox').check();
  await preliminary.click();
  await expect(page.getByText('Second approval · materialized plan')).toBeVisible();
  await expect(page.getByText('Pre-rollout checks')).toBeVisible();
  const rollout = page.getByRole('button', { name: 'Approve rollout' });
  await expect(rollout).toBeDisabled();
  await page.getByRole('checkbox').check();
  await rollout.click();
  expect(posted.some(path => path.endsWith('/operations/run-1/approve'))).toBe(true);
  await nav.getByRole('link', { name: 'Environments' }).click();
  await expect(page.getByText('1/1 ready')).toBeVisible();
  await nav.getByRole('link', { name: 'GitOps' }).click();
  await expect(page.getByText('Pending plan', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
