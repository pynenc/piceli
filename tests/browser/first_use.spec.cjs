const { test, expect } = require('./runtime.cjs');

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
