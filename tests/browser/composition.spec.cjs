// The in-cluster (forward-mode) UI: environments, one environment, sources and
// Sync, over a fake status and the fake Kubernetes API.
const { test, expect } = require('./session.cjs');

const fits = page => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);

test('launch opens the topology overview with accessible environment revisions and health', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page).toHaveURL(/\/composition\/overview$/);
  await expect(page.getByRole('heading', { name: 'Your delivery landscape', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Infrastructure topology', exact: true })).toBeVisible();
  await page.getByText('Environment inventory', { exact: true }).click();
  const main = page.getByRole('region', { name: 'Environment main' });
  await expect(main.getByText('3f9c2d1')).toBeVisible();
  await expect(main.getByText('1 rolling · 1 synced · 1 unchanged')).toBeVisible();
  await expect(main.getByText('Healthy').first()).toBeVisible();
  const branch = page.getByRole('region', { name: 'Environment wp-login' });
  await expect(branch.getByText('c1d2e3f')).toBeVisible();
  await expect(branch.getByText('Namespace pending')).toBeVisible();
  expect(await fits(page)).toBe(true);
  expect(errors).toEqual([]);
});

test('sync an environment and a component from the detail page', async ({ page }) => {
  await page.goto('/composition');
  const main = page.getByRole('region', { name: 'Environment main' });
  await main.getByRole('button', { name: 'Sync main' }).click();
  await expect(main.getByText(/Sync requested/)).toBeVisible();
  await main.getByRole('link', { name: 'Open environment' }).click();
  await expect(page.getByRole('heading', { name: 'main', exact: true })).toBeVisible();
  const components = page.getByRole('region', { name: 'Components' });
  await expect(components.getByText('Rolling')).toBeVisible();
  await expect(components.getByText('media', { exact: true })).toBeVisible();
  await components.getByRole('button', { name: 'Sync media' }).click();
  await expect(components.getByText(/Sync requested/)).toBeVisible();
  expect(await fits(page)).toBe(true);
  await page.getByRole('link', { name: 'Open workloads, pods and logs' }).click();
  await expect(page.getByRole('heading', { name: 'main', exact: true })).toBeVisible();
  await expect(page.getByText('worker', { exact: true }).first()).toBeVisible();
});

test('sources list their refs and an unknown environment is explained', async ({ page }) => {
  await page.goto('/composition/sources');
  const product = page.getByRole('region', { name: 'Source product' });
  await expect(product.getByText('https://git.example/shop/product.git')).toBeVisible();
  await expect(product.getByText('v1.4.0')).toBeVisible();
  expect(await fits(page)).toBe(true);
  await page.goto('/composition/environments/gone');
  await expect(page.getByText(/does not list that environment or component/)).toBeVisible();
});
