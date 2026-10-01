// Fixed journeys over the fake-API showcase server. Playwright records one
// video per test; tests/browser/run_clips.py turns them into WebP/GIF clips.
// The data is fake: nothing here reads a cluster, registry or credential.
const path = require('node:path');
const { test, expect } = require('./session.cjs');

const shots = process.env.PICELI_UI_CLIPS_SHOTS;
async function shot(page, name) {
  if (shots) await page.screenshot({ path: path.join(shots, `${name}.png`) });
}
const pause = (page, ms = 1200) => page.waitForTimeout(ms);

test('applications-overview', async ({ page }) => {
  await page.goto('/applications');
  await expect(page.getByRole('heading', { name: 'Applications', exact: true })).toBeVisible();
  await pause(page);
  await shot(page, 'applications');
  await page.getByRole('link', { name: 'Shop' }).click();
  await expect(page.getByRole('heading', { name: 'Shop', exact: true })).toBeVisible();
  await pause(page, 1800);
});

test('environments', async ({ page }) => {
  await page.goto('/environments');
  await expect(page.getByRole('heading', { name: 'Environments', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'wp-login', exact: true })).toBeVisible();
  await pause(page, 2000);
  await shot(page, 'environments');
  await pause(page, 1000);
});

test('gitops', async ({ page }) => {
  await page.goto('/gitops');
  await expect(page.getByRole('heading', { name: 'GitOps', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'wp-search', exact: true })).toBeVisible();
  await pause(page, 2000);
  await shot(page, 'gitops');
  await pause(page, 1000);
});

test('composition-environments', async ({ page }) => {
  await page.goto('/composition');
  await expect(page.getByRole('heading', { name: 'Environments', exact: true })).toBeVisible();
  const main = page.getByRole('region', { name: 'Environment main' });
  await expect(main.getByText('3f9c2d1')).toBeVisible();
  await pause(page, 1500);
  await shot(page, 'composition-environments');
  await main.getByRole('button', { name: 'Sync main' }).click();
  await expect(main.getByText(/Sync requested/)).toBeVisible();
  await pause(page, 1200);
  await main.getByRole('link', { name: 'Open environment' }).click();
  await expect(page.getByRole('region', { name: 'Components' })).toBeVisible();
  await pause(page, 1500);
  await shot(page, 'composition-environment');
  await page.goto('/composition/sources');
  await expect(page.getByRole('region', { name: 'Source product' })).toBeVisible();
  await pause(page, 1200);
  await shot(page, 'composition-sources');
});

test('pipeline-plan-approval', async ({ page }) => {
  await page.goto('/pipeline');
  await expect(page.getByRole('heading', { name: 'Pipeline', exact: true })).toBeVisible();
  await pause(page, 800);
  await page.getByRole('button', { name: 'Prepare new plan' }).click();
  await expect(page.getByText('Exact combined plan')).toBeVisible({ timeout: 30000 });
  await pause(page, 1500);
  const confirmation = page.getByRole('checkbox').first();
  await confirmation.check();
  await pause(page, 1500);
  await shot(page, 'pipeline-plan');
  // The clip stops before the approval: nothing is deployed.
  await expect(page.getByRole('button', { name: /^Approve/ })).toBeEnabled();
  await pause(page, 1000);
});
