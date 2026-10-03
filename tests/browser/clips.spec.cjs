// Fixed journeys over the fake-API showcase server. Playwright records one
// video per test; tests/browser/run_clips.py turns them into WebP/GIF clips.
// The data is fake: nothing here reads a cluster, registry or credential.
const path = require('node:path');
const { test, expect } = require('./session.cjs');

const shots = process.env.PICELI_UI_CLIPS_SHOTS;
async function shot(page, name) {
  if (shots) await page.screenshot({ path: path.join(shots, `${name}.png`), fullPage: true });
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

test('composition-overview', async ({ page }) => {
  await page.goto('/composition/overview?environment=main');
  await expect(page.getByRole('heading', { name: 'System schematic', exact: true })).toBeVisible();
  await pause(page);
  await page.getByRole('button', { name: 'Inspect media in main' }).click();
  await expect(page.getByRole('complementary', { name: 'Selected component' }).getByRole('heading', { name: 'media', exact: true })).toBeVisible();
  await pause(page, 1600);
  await shot(page, 'composition-overview');
  await page.getByRole('combobox', { name: 'Environment', exact: true }).selectOption('wp-login');
  await expect(page.getByRole('button', { name: 'Inspect web in wp-login' })).toBeVisible();
  await pause(page, 1400);
});

test('compact-source-inventory', async ({ page }) => {
  await page.goto('/composition/sources');
  const product = page.getByRole('region', { name: 'Source product', exact: true });
  await expect(product).toBeVisible();
  await pause(page, 1300);
  await shot(page, 'compact-sources');
  const revision = product.getByLabel('Revision for product ref main', { exact: true });
  await revision.locator('summary').click();
  await expect(revision.getByText('3f9c2d1e8a7b4c5d6e0f1a2b3c4d5e6f7a8b9c0d', { exact: true })).toBeVisible();
  await pause(page, 1300);
  await shot(page, 'source-full-revision');
  await page.getByRole('searchbox', { name: 'Filter sources', exact: true }).fill('media');
  await expect(product).toHaveCount(0);
  await pause(page, 1000);
  await page.getByRole('link', { name: 'View assets topology in main', exact: true }).click();
  await expect(page.getByRole('complementary', { name: 'Selected source', exact: true }).getByRole('heading', { name: 'assets', exact: true })).toBeVisible();
  await pause(page, 1300);
});

test('resource-relationships', async ({ page }) => {
  await page.goto('/applications/shop/resources?view=relationships');
  await expect(page.getByText('Observed ownership', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Fit graph' }).click();
  await pause(page, 1400);
  await shot(page, 'resource-relationships');
  await page.getByRole('combobox', { name: 'Focus resource' }).selectOption({ label: 'ReplicaSet / api-release' });
  await expect(page.getByRole('status', { name: 'Graph focus' })).toContainText('1 observed owner · 2 direct children');
  await pause(page, 1200);
  await page.getByRole('button', { name: 'Inspect Deployment api in piceli-test', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Resource details', exact: true });
  await expect(dialog.getByRole('heading', { name: 'api', exact: true })).toBeVisible();
  await pause(page, 1600);
  await shot(page, 'resource-inspector');
  await dialog.getByRole('link', { name: 'Inspect related ReplicaSet api-release in piceli-test', exact: true }).click();
  await expect(dialog.getByRole('heading', { name: 'api-release', exact: true })).toBeVisible();
  await pause(page, 1000);
  await dialog.getByRole('link', { name: 'Inspect related Pod api-release-1 in piceli-test', exact: true }).click();
  await expect(dialog.getByRole('table', { name: 'Reported conditions' })).toBeVisible();
  await pause(page, 1200);
  await shot(page, 'resource-conditions');
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await pause(page);
});

test('environment-versions', async ({ page }) => {
  await page.goto('/composition/overview?view=versions&baseline=main&compare=wp-login');
  await expect(page.getByRole('heading', { name: 'What changes between environments?', exact: true })).toBeVisible();
  await pause(page, 1600);
  await shot(page, 'environment-versions');
  const web = page.getByRole('row').filter({ has: page.getByRole('rowheader', { name: /^web/ }) });
  await web.getByText('Exact identities', { exact: true }).first().click();
  await pause(page, 1400);
  await web.getByRole('button', { name: 'Inspect target', exact: false }).click();
  await expect(page.getByRole('complementary', { name: 'Selected component' }).getByRole('heading', { name: 'web', exact: true })).toBeVisible();
  await pause(page, 1300);
});

test('environment-attention', async ({ page }) => {
  await page.goto('/composition/overview?view=attention');
  await expect(page.getByRole('region', { name: 'Decisions waiting', exact: true }).getByText('Plan awaiting approval', { exact: true })).toBeVisible();
  await pause(page, 1700);
  await shot(page, 'environment-attention');
  await page.getByRole('region', { name: 'In progress & stopped', exact: true }).getByRole('listitem').filter({ hasText: 'main / media' }).getByRole('link', { name: 'Inspect', exact: true }).click();
  await expect(page.getByRole('complementary', { name: 'Selected component' }).getByRole('heading', { name: 'media', exact: true })).toBeVisible();
  await pause(page, 1600);
});

test('workspace-search', async ({ page }) => {
  await page.goto('/applications');
  await expect(page.getByRole('link', { name: 'Shop', exact: true })).toBeVisible();
  await page.keyboard.press('Control+k');
  await page.getByRole('searchbox', { name: 'Search destinations', exact: true }).fill('media');
  const result = page.getByRole('link', { name: 'Component: media, main · assets', exact: true });
  await expect(result).toBeVisible();
  await pause(page, 1700);
  await shot(page, 'workspace-search');
  await result.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('complementary', { name: 'Selected component' }).getByRole('heading', { name: 'media', exact: true })).toBeVisible();
  await pause(page, 1700);
});

test('recorded-activity-and-plan', async ({ page }) => {
  await page.goto('/applications/shop/activity');
  const failed = page.getByRole('article', { name: 'Run showcase-failed', exact: true });
  await expect(failed).toBeVisible();
  await page.getByRole('group', { name: 'Run status filter' }).getByRole('button', { name: /^Needs attention/ }).click();
  await pause(page, 1500);
  await shot(page, 'recorded-activity');
  await failed.getByRole('link', { name: 'Review plan', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Review deployment', exact: true })).toBeVisible();
  await page.getByRole('navigation', { name: 'Changed resources' }).getByRole('button', { name: /^api Service/ }).click();
  await expect(page.getByLabel('After /spec/ports/0/port', { exact: true })).toHaveText('80');
  await pause(page, 1700);
  await shot(page, 'recorded-plan');
  await expect(page.getByRole('button', { name: /^Deploy to / })).toBeDisabled();
  await pause(page, 800);
});

test('recorded-revision-differences', async ({ page }) => {
  await page.goto('/applications/shop/activity?history=revisions&compareFrom=showcase-plan-previous&compareTo=showcase-plan');
  const differences = page.getByRole('region', { name: 'Revision differences', exact: true });
  await expect(differences.getByRole('heading', { name: '3 resources differ', exact: true })).toBeVisible();
  await pause(page, 1200);
  await differences.getByRole('searchbox', { name: 'Search changes', exact: true }).fill('/spec/template/spec/containers/0/image');
  await expect(differences.getByLabel('After /spec/template/spec/containers/0/image', { exact: true })).toHaveText(JSON.stringify('registry.example/shop/api:v1.5.0'));
  await pause(page, 1500);
  await shot(page, 'recorded-revisions');
  await page.getByRole('link', { name: 'Open To plan', exact: true }).click();
  const order = page.getByRole('region', { name: 'Execution order', exact: true });
  await order.getByRole('button', { name: 'Inspect planned action 3: Deployment api', exact: true }).click();
  await expect(order.getByRole('region', { name: 'Selected action details', exact: true })).toContainText('ConfigMap / piceli-test / public-settings');
  await pause(page, 1800);
  await shot(page, 'deployment-phases');
});

test('recorded-execution-journal', async ({ page }) => {
  await page.goto('/runs/showcase-failed');
  const order = page.getByRole('region', { name: 'Execution order', exact: true });
  await order.getByRole('button', { name: 'Inspect recorded action 3: Deployment api', exact: true }).click();
  await expect(order.getByRole('region', { name: 'Selected action details', exact: true })).toContainText('Readiness has not been recorded');
  await pause(page, 1700);
  await shot(page, 'recorded-execution');
  const journal = page.getByRole('region', { name: 'Execution journal and logs', exact: true });
  await journal.getByRole('button', { name: /^Captured logs/ }).click();
  await expect(journal.getByLabel('Captured output for api-preview / api', { exact: true })).toContainText('Preview fixture: readiness probe did not succeed before the deadline');
  await pause(page, 1800);
  await shot(page, 'recorded-journal');
  await journal.getByText('Raw recorded journal', { exact: true }).click();
  await expect(journal.getByLabel('Raw recorded journal', { exact: true })).toContainText('preview-execution-failed');
  await pause(page, 1300);
});

test('deployment-history-archive', async ({ page }) => {
  await page.goto('/delivery?application=shop');
  const unused = page.getByRole('row', { name: 'Plan showcase-plan-unused', exact: true });
  await expect(unused.getByText('No recorded run', { exact: true })).toBeVisible();
  await pause(page, 1300);
  await shot(page, 'deployment-history');
  await page.getByRole('combobox', { name: 'Filter plans', exact: true }).selectOption('no-runs');
  await unused.getByText('Full revision', { exact: true }).click();
  await pause(page, 1300);
  await shot(page, 'unexecuted-plan');
  await unused.getByRole('link', { name: 'Open plan', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Resource change workbench', exact: true }).getByLabel('After /spec/replicas', { exact: true })).toHaveText('4');
  await pause(page, 1300);
  await page.goto('/delivery?application=shop&deliveryView=runs');
  await page.getByRole('article', { name: 'Run showcase-failed', exact: true }).getByRole('link', { name: 'Open logs', exact: true }).click();
  await expect(page.getByLabel('Captured output for api-preview / api', { exact: true })).toContainText('Preview fixture: readiness probe did not succeed before the deadline');
  await pause(page, 1400);
  await shot(page, 'direct-recorded-logs');
});

test('cluster-overview', async ({ page }) => {
  await page.goto('/cluster');
  await expect(page.getByRole('heading', { name: 'Cluster', exact: true })).toBeVisible();
  await expect(page.getByText('Restart k3s on this node')).toBeVisible();
  await pause(page, 1500);
  await shot(page, 'cluster');
});

test('named-environment-actions', async ({ page }) => {
  await page.goto('/composition/environments/preview');
  await expect(page.getByRole('heading', { name: 'preview', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Approve', exact: true }).click();
  await expect(page.getByRole('region', { name: 'approve review' })).toContainText('sha256:');
  await pause(page, 1300);
  await shot(page, 'named-environment-approve');
  await page.getByRole('button', { name: 'Cancel' }).click();
  await page.getByRole('button', { name: 'Promote', exact: true }).click();
  await page.getByRole('combobox', { name: 'Published branch and commit' }).selectOption({ index: 1 });
  await expect(page.getByRole('region', { name: 'promote review' })).toContainText('The controller may still require');
  await pause(page, 1300);
  await shot(page, 'named-environment-promote');
});

test('idle-stopped-environment', async ({ page }) => {
  await page.goto('/composition/environments/wp-idle');
  await expect(page.getByText('Idle-stopped since')).toBeVisible();
  await page.getByRole('button', { name: 'Wake' }).click();
  await expect(page.getByRole('region', { name: 'wake review' })).toContainText('Request a sync');
  await pause(page, 1300);
  await shot(page, 'idle-stopped-environment');
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

test('logs-workspace', async ({ page }) => {
  await page.goto('/composition/environments/main');
  await page.getByRole('main').getByRole('link', { name: 'Logs', exact: true }).first().click();
  const lines = page.getByRole('region', { name: 'Container log lines' });
  await expect(lines.getByText('INFO starting api v1.4.0').first()).toBeVisible();
  await pause(page, 1500);
  await shot(page, 'logs-workspace');
  await page.getByRole('checkbox', { name: /^Error/ }).click();
  await page.getByRole('checkbox', { name: /^Warning/ }).click();
  await expect(lines.getByText('INFO starting api v1.4.0')).toHaveCount(0);
  await pause(page, 1200);
  await lines.getByText(/ERROR request failed/).first().click();
  await expect(page.getByRole('region', { name: 'Selected log line' })).toBeVisible();
  await pause(page, 1600);
});

test('forwards-workspace', async ({ page }) => {
  await page.goto('/applications/shop/resources?view=table');
  await page.getByRole('button', { name: 'Inspect Service api in piceli-test', exact: true }).click();
  await page.getByRole('dialog', { name: 'Resource details' }).getByRole('link', { name: 'Forward', exact: true }).click();
  const start = page.getByRole('region', { name: 'Start a forward' });
  await start.getByRole('spinbutton').fill('18491');
  await pause(page, 900);
  await start.getByRole('button', { name: 'Start forward' }).click();
  const active = page.getByRole('region', { name: 'Active forwards' });
  await expect(active.getByRole('link', { name: 'http://127.0.0.1:18491' })).toBeVisible();
  await pause(page, 1500);
  await shot(page, 'forwards-workspace');
  await active.getByRole('button', { name: 'Stop forward api 18491' }).click();
  await expect(active.getByRole('link', { name: 'http://127.0.0.1:18491' })).toHaveCount(0);
  await pause(page, 1000);
});

test('navigation-badges', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/composition/overview?environment=main');
  const nav = page.getByRole('navigation', { name: 'Main navigation' });
  await expect(nav.getByLabel(/approvals waiting$/)).toBeVisible();
  await pause(page, 1200);
  await shot(page, 'overview-compact');
  await nav.getByRole('button', { name: 'Expand Environments' }).click();
  await nav.getByRole('link', { name: 'preview', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'preview', exact: true })).toBeVisible();
  await pause(page, 1500);
});
