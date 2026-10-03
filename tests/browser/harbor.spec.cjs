// Harbor interactions over the existing disposable showcase. Production data
// contracts and approvals are exercised in the other browser suites.
const { test, expect } = require('./session.cjs');
const path = require('node:path');
const { writeFile } = require('node:fs/promises');

const assetsCommit = 'b7e4a1c9d2f3081726354a5b6c7d8e9f0a1b2c3d';
const branchCommit = 'c1d2e3f4a5b60718293a4b5c6d7e8f9001122334';
const fits = page => expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
const query = page => new URL(page.url()).searchParams;
const colorScheme = page => page.evaluate(() => getComputedStyle(document.documentElement).colorScheme);
async function shot(page, testInfo, name) {
  const directory = process.env.PICELI_UI_HARBOR_SHOTS;
  if (directory) await page.screenshot({ path: path.join(directory, `${testInfo.project.name}-${name}.png`), fullPage: true });
}

test('overview selects reported components by keyboard and restores the address', async ({ page }, testInfo) => {
  await page.goto('/composition/overview?environment=main');
  await expect(page.getByRole('heading', { name: 'Your delivery landscape' })).toBeVisible();
  const media = page.getByRole('button', { name: 'Inspect media in main' });
  await media.focus();
  await page.keyboard.press('Enter');
  const inspector = page.getByRole('complementary', { name: 'Selected component' });
  await expect(inspector.getByRole('heading', { name: 'media', exact: true })).toBeVisible();
  await expect(inspector.getByText(assetsCommit, { exact: true })).toBeVisible();
  await expect(media).toHaveAttribute('aria-pressed', 'true');
  expect(query(page).get('environment')).toBe('main');
  expect(query(page).get('component')).toBe('media');
  await fits(page);
  await shot(page, testInfo, 'overview');
  await page.reload();
  await expect(inspector.getByRole('heading', { name: 'media', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Inspect media in main' })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('combobox', { name: 'Environment', exact: true }).selectOption('wp-login');
  await expect(inspector.getByRole('heading', { name: 'web', exact: true })).toBeVisible();
  await expect(inspector.getByText(branchCommit, { exact: true })).toBeVisible();
  expect(query(page).get('environment')).toBe('wp-login');
  expect(query(page).has('component')).toBe(false);
  await page.getByRole('combobox', { name: 'Environment', exact: true }).selectOption('wp-idle');
  await expect(page.getByRole('heading', { name: 'No components reported', exact: true })).toBeVisible();
  await expect(inspector.getByRole('heading', { name: 'No component selected' })).toBeVisible();
  await fits(page);
});

test('sources associate only reported components and environments', async ({ page }, testInfo) => {
  await page.goto('/composition/sources');
  const product = page.getByRole('region', { name: 'Source product', exact: true });
  await expect(product.getByText('https://git.example/shop/product.git', { exact: true })).toBeVisible();
  const assets = page.getByRole('region', { name: 'Destinations for assets', exact: true });
  await expect(assets.getByText('media', { exact: true })).toBeVisible();
  await expect(assets.getByRole('link', { name: 'Environment main', exact: true })).toBeVisible();
  await expect(assets.getByRole('link', { name: 'Environment wp-login', exact: true })).toBeVisible();
  await expect(assets.getByText('No component source reported', { exact: true })).toBeVisible();
  await expect(assets.getByRole('link', { name: 'Environment preview', exact: true })).toHaveCount(0);
  await fits(page);
  await shot(page, testInfo, 'compact-sources');
  if (testInfo.project.name === 'desktop') {
    const lastDestination = assets.getByRole('link', { name: 'Environment wp-login', exact: true });
    const bounds = await lastDestination.boundingBox();
    expect(bounds.y + bounds.height).toBeLessThan(page.viewportSize().height);
  }
  const revision = product.getByLabel('Revision for product ref main', { exact: true });
  await revision.locator('summary').focus();
  await page.keyboard.press('Enter');
  await expect(revision.getByText('3f9c2d1e8a7b4c5d6e0f1a2b3c4d5e6f7a8b9c0d', { exact: true })).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(revision.getByText('3f9c2d1e8a7b4c5d6e0f1a2b3c4d5e6f7a8b9c0d', { exact: true })).toBeHidden();
  const search = page.getByRole('searchbox', { name: 'Filter sources', exact: true });
  await search.fill('media');
  await expect(product).toHaveCount(0);
  await expect(assets.getByRole('link', { name: 'Inspect media in main', exact: true })).toBeVisible();
  expect(query(page).get('sourceSearch')).toBe('media');
  await page.reload();
  await expect(search).toHaveValue('media');
  await expect(product).toHaveCount(0);
  await page.getByRole('button', { name: 'Clear source filter', exact: true }).click();
  await expect(product).toBeVisible();
  await expect(search).toHaveValue('');
  await assets.getByRole('link', { name: 'View assets topology in main', exact: true }).click();
  await expect(page.getByRole('complementary', { name: 'Selected source', exact: true }).getByRole('heading', { name: 'assets', exact: true })).toBeVisible();
  expect(query(page).get('environment')).toBe('main');
  expect(query(page).get('source')).toBe('assets');
  await page.goto('/composition/sources');
  await assets.getByRole('link', { name: 'Environment main', exact: true }).click();
  await expect(page).toHaveURL(/\/composition\/environments\/main$/);
  await expect(page.getByRole('region', { name: 'Components' }).getByText('media', { exact: true })).toBeVisible();
  await fits(page);
  if (testInfo.project.name === 'desktop' && process.env.PICELI_UI_HARBOR_SHOTS) {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto('/composition/sources');
    await expect(product).toBeVisible();
    const measurements = await page.locator('.composition-sources').evaluate(element => ({
      viewport: { width: innerWidth, height: innerHeight },
      documentHeight: document.documentElement.scrollHeight,
      contentHeight: element.getBoundingClientRect().height,
      sourceHeights: [...element.querySelectorAll('.source-inventory-row')].map(row => ({ source: row.getAttribute('aria-label'), height: row.getBoundingClientRect().height })),
    }));
    await shot(page, testInfo, 'sources-density');
    await writeFile(path.join(process.env.PICELI_UI_HARBOR_SHOTS, 'sources-density.json'), JSON.stringify(measurements, null, 2));
  }
});

test('application Cards and Table preserve search and target through reload', async ({ page }) => {
  await page.goto('/applications');
  await expect(page.getByRole('link', { name: 'Shop', exact: true })).toBeVisible();
  await page.getByRole('searchbox', { name: 'Search applications' }).fill('Shop');
  const target = page.getByRole('combobox', { name: 'Target', exact: true });
  await target.selectOption({ index: 1 });
  const selectedTarget = await target.inputValue();
  expect(selectedTarget).not.toBe('');
  const layouts = page.getByRole('group', { name: 'Application layout' });
  await layouts.getByRole('button', { name: 'Table', exact: true }).click();
  await expect(layouts.getByRole('button', { name: 'Table', exact: true })).toHaveAttribute('aria-pressed', 'true');
  expect(query(page).get('q')).toBe('Shop');
  expect(query(page).get('target')).toBe(selectedTarget);
  expect(query(page).get('layout')).toBe('table');
  await expect(page.getByRole('region', { name: 'Applications', exact: true }).getByRole('link', { name: 'Shop', exact: true })).toBeVisible();
  await fits(page);
  await page.reload();
  await expect(layouts.getByRole('button', { name: 'Table', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(target).toHaveValue(selectedTarget);
  await expect(page.getByRole('searchbox', { name: 'Search applications' })).toHaveValue('Shop');
  await layouts.getByRole('button', { name: 'Cards', exact: true }).click();
  expect(query(page).get('q')).toBe('Shop');
  expect(query(page).get('target')).toBe(selectedTarget);
  expect(query(page).has('layout')).toBe(false);
  await page.reload();
  await expect(layouts.getByRole('button', { name: 'Cards', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('link', { name: 'Shop', exact: true })).toBeVisible();
  await fits(page);
});

test('appearance persists explicit choices and follows live system changes', async ({ page }, testInfo) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/applications');
  const appearance = page.getByRole('combobox', { name: 'Appearance', exact: true });
  await expect(appearance).toHaveValue('system');
  await expect.poll(() => colorScheme(page)).toBe('light');
  await appearance.selectOption('dark');
  await expect.poll(() => colorScheme(page)).toBe('dark');
  await page.reload();
  await expect(appearance).toHaveValue('dark');
  await expect.poll(() => colorScheme(page)).toBe('dark');
  await expect(page.getByRole('link', { name: 'Shop', exact: true })).toBeVisible();
  await fits(page);
  await shot(page, testInfo, 'dark-applications');
  await appearance.selectOption('light');
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect.poll(() => colorScheme(page)).toBe('light');
  await page.reload();
  await expect(appearance).toHaveValue('light');
  await appearance.selectOption('system');
  await expect.poll(() => colorScheme(page)).toBe('dark');
  await page.emulateMedia({ colorScheme: 'light' });
  await expect.poll(() => colorScheme(page)).toBe('light');
  await page.reload();
  await expect(appearance).toHaveValue('system');
  await expect.poll(() => colorScheme(page)).toBe('light');
  await fits(page);
});

test('Relationships keeps every observed resource and returns focus after inspection', async ({ page }, testInfo) => {
  await page.goto('/applications/shop/overview');
  await expect(page.getByText('Observed ownership', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Table', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Table', exact: true })).toHaveAttribute('aria-pressed', 'true');
  expect(query(page).get('view')).toBe('table');
  await page.reload();
  await expect(page.getByRole('button', { name: 'Table', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.goto('/applications/shop/resources');
  const list = page.getByRole('region', { name: 'Resource list', exact: true });
  const inspect = page.getByRole('button', { name: 'Inspect Deployment api in piceli-test', exact: true });
  await expect(inspect).toBeVisible();
  const tableIdentities = await list.getByRole('button', { name: /^Inspect / }).evaluateAll(buttons => buttons.map(button => button.getAttribute('aria-label')).sort());
  await page.getByRole('button', { name: 'Relationships', exact: true }).click();
  await expect(page.getByText('Observed ownership', { exact: true })).toBeVisible();
  expect(query(page).get('view')).toBe('relationships');
  expect(await list.getByRole('button', { name: /^Inspect / }).evaluateAll(buttons => buttons.map(button => button.getAttribute('aria-label')).sort())).toEqual(tableIdentities);
  const response = await page.request.get('/api/v1/applications/shop/resources');
  expect(response.ok()).toBe(true);
  const inventory = await response.json();
  const uids = new Set(inventory.items.map(resource => resource.identity.uid));
  const references = inventory.items.reduce((count, resource) => count + [...new Set(resource.owner_uids || [])].filter(uid => uids.has(uid)).length, 0);
  expect(references).toBeGreaterThan(0);
  await expect(page.getByText(`${tableIdentities.length} resources · ${references} references`, { exact: true })).toBeVisible();
  await inspect.focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('button', { name: 'Inspect ReplicaSet api-release in piceli-test', exact: true })).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('button', { name: 'Inspect Pod api-release-1 in piceli-test', exact: true })).toBeFocused();
  await page.getByRole('combobox', { name: 'Focus resource' }).selectOption({ label: 'ReplicaSet / api-release' });
  await expect(page.getByRole('status', { name: 'Graph focus' })).toContainText('1 observed owner · 2 direct children');
  await page.getByRole('button', { name: 'Fit graph' }).click();
  await shot(page, testInfo, 'resource-graph');
  await page.getByRole('button', { name: 'Reset zoom' }).click();
  await expect(page.getByLabel('Graph scale')).toHaveText('100%');
  await inspect.focus();
  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: 'Resource details', exact: true });
  await expect(dialog.getByRole('heading', { name: 'api', exact: true })).toBeVisible();
  await expect(dialog.getByText('Deployment · fake / piceli-test', { exact: true })).toBeVisible();
  expect(query(page).get('resource')).toBeTruthy();
  await expect(dialog.getByRole('button', { name: 'Summary', exact: true })).toHaveAttribute('aria-current', 'page');
  // The showcase session does not grant manifests; the diagram must preserve
  // the same capability-gated inspector as Table.
  await expect(dialog.getByRole('button', { name: 'Manifest', exact: true })).toHaveCount(0);
  expect(query(page).get('panel')).toBe('summary');
  const configuration = dialog.getByRole('region', { name: 'Reported configuration', exact: true });
  await expect(configuration.getByText('registry.example/shop/api:v1.4.0', { exact: true })).toBeVisible();
  await expect(configuration.getByRole('list', { name: 'Reported ports' })).toHaveText('8080');
  const replica = inventory.items.find(resource => resource.identity.kind === 'ReplicaSet');
  const relatedReplica = dialog.getByRole('link', { name: 'Inspect related ReplicaSet api-release in piceli-test', exact: true });
  expect(new URL(await relatedReplica.getAttribute('href'), page.url()).searchParams.get('resource')).toBe(replica.id);
  await relatedReplica.click();
  await expect(dialog.getByRole('heading', { name: 'api-release', exact: true })).toBeFocused();
  await expect(dialog.getByRole('link', { name: 'Inspect related Deployment api in piceli-test', exact: true })).toBeVisible();
  await dialog.getByRole('link', { name: 'Inspect related Pod api-release-1 in piceli-test', exact: true }).click();
  await expect(dialog.getByRole('heading', { name: 'api-release-1', exact: true })).toBeFocused();
  const conditions = dialog.getByRole('table', { name: 'Reported conditions' });
  await expect(conditions.getByRole('cell', { name: 'Ready', exact: true })).toBeVisible();
  await expect(conditions.getByRole('cell', { name: 'True', exact: true })).toBeVisible();
  await shot(page, testInfo, 'resource-inspector');
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(inspect).toBeFocused();
  expect(query(page).get('view')).toBe('relationships');
  expect(query(page).has('resource')).toBe(false);
  expect(query(page).has('panel')).toBe(false);
  await fits(page);
});
