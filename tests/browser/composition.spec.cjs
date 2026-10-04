// The in-cluster (forward-mode) UI: environments, one environment, sources and
// Sync, over a fake status and the fake Kubernetes API.
const { test, expect } = require('./session.cjs');

const fits = page => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);
// Opt-in: PICELI_UI_SHOTS=DIR keeps the overview screenshots for review (default: the run's temporary output).
const shot = (testInfo, name) => process.env.PICELI_UI_SHOTS ? `${process.env.PICELI_UI_SHOTS}/${testInfo.project.name}-${name}` : testInfo.outputPath(name);

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

test('deployment history shows each environment’s runs newest first from the controller', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await page.getByRole('link', { name: 'Deployment history' }).click();
  await expect(page).toHaveURL(/\/delivery$/);
  await expect(page.getByRole('heading', { name: 'Deployment history', exact: true })).toBeVisible();
  const runs = page.getByRole('list', { name: 'Runs of main' });
  const newest = runs.locator(':scope > li').first();
  await expect(newest).toContainText('Checks failed; the release keeps running');
  await expect(newest).toContainText('deliberate-failure');
  await expect(newest).toContainText('sh exited 1, expected 0');
  const deployed = runs.locator(':scope > li').nth(1);
  await deployed.locator('summary').click();
  await expect(deployed).toContainText('Deployed; rolled web');
  await expect(deployed).toContainText('push product/main');
  await expect(deployed).toContainText('piceli gitops approve (CLI)');
  await expect(deployed.getByRole('region', { name: 'Run images' })).toContainText('built');
  await expect(deployed.getByRole('region', { name: 'Run plan' })).toContainText('Deployment/web');
  await expect(deployed.getByRole('region', { name: 'Run sources and commits' })).toContainText('3f9c2d1e8a7b');
  await expect(runs.locator(':scope > li').nth(2)).toContainText("Deployed; rolled nothing");
  expect(await fits(page)).toBe(true);
  await page.getByLabel('Environment').selectOption('wp-login');
  await expect(page).toHaveURL(/environment=wp-login/);
  await expect(page.getByLabel('Failure log tail')).toContainText('missing script: build');
  await page.reload();
  await expect(page.getByLabel('Failure log tail')).toContainText('missing script: build');
  expect(await fits(page)).toBe(true);
  expect(errors).toEqual([]);
});

test('an environment shows its history, verification state and the pending plan before approval', async ({ page }) => {
  await page.goto('/composition/environments/main');
  const history = page.getByRole('region', { name: 'Deployment history' });
  await expect(history.getByRole('list', { name: 'Runs of main' }).locator(':scope > li')).toHaveCount(3);
  await page.goto('/composition/environments/stage');
  await expect(page.getByText('Checks verification failed')).toBeVisible();
  await expect(page.getByText('deliberate-failure')).toBeVisible();
  const clusters = page.getByRole('list', { name: 'Clusters of stage' });
  await expect(clusters.locator(':scope > li')).toHaveCount(2);
  await expect(clusters).toContainText('cluster-unreachable');
  await expect(clusters).toContainText('last contact');
  await page.goto('/composition/environments/rc');
  await page.getByRole('button', { name: 'Approve' }).click();
  const review = page.getByRole('region', { name: 'approve review' });
  await expect(review.getByText(/sha256:d{64}/).first()).toBeVisible();
  await expect(review.getByLabel('Pending plan changes')).toContainText('Deployment/web');
  await expect(review.getByLabel('Pending plan changes')).toContainText('1 update · 4 no-op');
  expect(await fits(page)).toBe(true);
  await review.getByRole('checkbox').check();
  await review.getByRole('button', { name: 'Confirm approve' }).click();
  await expect(page.getByText('Request recorded')).toBeVisible();
});

test('promote offers the published branch heads and sync and the cluster show the controller’s facts', async ({ page }) => {
  await page.goto('/composition/environments/rc');
  await page.getByRole('button', { name: 'Promote' }).click();
  const review = page.getByRole('region', { name: 'promote review' });
  await review.getByLabel('Published branch and commit').selectOption({ index: 1 });
  await expect(review.getByText(/This requests/)).toBeVisible();
  await page.goto('/cluster');
  const registry = page.getByRole('region', { name: 'In-cluster registry' });
  await expect(registry).toContainText('0.52 GiB');
  await expect(registry).toContainText('by the GitOps controller');
  await expect(page.getByRole('region', { name: 'Cluster nodes' })).toContainText('node-a');
  expect(await fits(page)).toBe(true);
});

test('overview: the topology spans the width, details sit below it, pinned cards side by side', async ({ page }, testInfo) => {
  await page.goto('/');
  const map = page.getByRole('region', { name: 'Component dependencies', exact: true });
  const details = page.getByRole('region', { name: 'Selected details', exact: true });
  await expect(details).toBeVisible();
  const mapBox = await map.boundingBox();
  const detailsBox = await details.boundingBox();
  expect(detailsBox.y).toBeGreaterThanOrEqual(mapBox.y + mapBox.height - 1);
  expect(Math.abs(detailsBox.width - mapBox.width)).toBeLessThan(2);
  await page.screenshot({ path: shot(testInfo, 'overview-one-card.png'), fullPage: true });

  const source = page.getByRole('button', { name: /^Select source / }).first();
  await source.click({ modifiers: ['Shift'] });
  await expect(details).toHaveAttribute('data-cards', '2');
  const cards = details.getByRole('complementary');
  await expect(cards).toHaveCount(2);
  const [first, second] = [await cards.nth(0).boundingBox(), await cards.nth(1).boundingBox()];
  if (testInfo.project.name === 'phone') expect(second.y).toBeGreaterThan(first.y);
  else if (testInfo.project.name === 'desktop') { expect(Math.abs(second.y - first.y)).toBeLessThan(2); expect(second.x).toBeGreaterThan(first.x + first.width - 1); }
  expect(await fits(page)).toBe(true);
  await page.screenshot({ path: shot(testInfo, 'overview-two-cards.png'), fullPage: true });
});
