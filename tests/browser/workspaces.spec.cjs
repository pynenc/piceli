// The 0.14.7 workspaces on the disposable showcase (fake Kubernetes API,
// fixture forwards that are real loopback listeners, two saved profiles).
const { test, expect } = require('./session.cjs');

const query = page => new URL(page.url()).searchParams;
const fits = page => expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
const narrow = testInfo => testInfo.project.name === 'phone';
const ports = { desktop: 18431, wide: 18432, tablet: 18433, phone: 18434 };

test('overview schematic has no inner scroll area and compact cards', async ({ page }, testInfo) => {
  await page.goto('/composition/overview?environment=main');
  const viewport = page.getByRole('region', { name: 'Infrastructure topology' });
  await expect(page.getByRole('button', { name: 'Inspect media in main', exact: true })).toBeVisible();
  const box = await viewport.evaluate(element => ({ sh: element.scrollHeight, ch: element.clientHeight, sw: element.scrollWidth, cw: element.clientWidth, overflowY: getComputedStyle(element).overflowY }));
  // Never a vertical inner scroll; sideways panning only on a phone.
  expect(box.sh).toBeLessThanOrEqual(box.ch);
  expect(box.overflowY).toBe('hidden');
  if (!narrow(testInfo)) expect(box.sw).toBeLessThanOrEqual(box.cw);
  const card = await page.getByRole('button', { name: 'Inspect media in main', exact: true }).boundingBox();
  expect(card.height).toBeLessThanOrEqual(70);
  const stop = await page.getByRole('button', { name: 'Show environment main', exact: true }).boundingBox();
  expect(stop.height).toBeLessThanOrEqual(90);
  if (testInfo.project.name === 'desktop') {
    // At 1440×900 the whole schematic is on the first screen.
    const graph = await viewport.boundingBox();
    expect(graph.y + graph.height).toBeLessThanOrEqual(900);
  }
  await fits(page);
});

test('navigation shows alert counts and keyboard sub-menus', async ({ page }, testInfo) => {
  await page.goto('/composition/environments/main');
  const nav = page.getByRole('navigation', { name: 'Main navigation' });
  await expect(nav.getByTitle(/approvals waiting$/)).toBeVisible();
  await expect(nav.getByTitle(/registry warnings$/)).toBeVisible();
  await expect(nav.getByRole('link', { name: 'Logs', exact: true })).toBeVisible();
  if (narrow(testInfo)) return; // phones keep a single scrolling row without sub-menus
  const toggle = nav.getByRole('button', { name: 'Collapse Environments' });
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(nav.getByRole('link', { name: 'preview', exact: true })).toBeVisible();
  await toggle.focus();
  await page.keyboard.press('Enter');
  await expect(nav.getByRole('link', { name: 'preview', exact: true })).toBeHidden();
  await page.reload();
  await expect(nav.getByRole('button', { name: 'Expand Environments' })).toHaveAttribute('aria-expanded', 'false');
  await nav.getByRole('button', { name: 'Expand Cluster' }).click();
  await nav.getByRole('link', { name: 'Registry', exact: true }).click();
  await expect(page).toHaveURL(/\/cluster#registry$/);
  await expect(page.getByRole('region', { name: 'In-cluster registry' })).toBeVisible();
});

test('logs workspace: environment deep link, filters and live tail in the address', async ({ page }) => {
  await page.goto('/composition/environments/main');
  await page.getByRole('main').getByRole('link', { name: 'Logs', exact: true }).first().click();
  await expect(page).toHaveURL(/\/logs\?scope=env-/);
  const lines = page.getByRole('region', { name: 'Container log lines' });
  await expect(lines.getByText('INFO starting api v1.4.0').first()).toBeVisible();
  await expect(lines).not.toContainText('fixture-secret-value');
  await expect(lines.getByText('ERROR request failed: upstream timeout token=[REDACTED]')).toBeVisible();
  // Filters are URL-backed: wait for the router to commit the change.
  await page.getByRole('checkbox', { name: /^Error/ }).click();
  await expect(page.getByRole('checkbox', { name: /^Error/ })).toBeChecked();
  await expect(lines.getByText('INFO starting api v1.4.0')).toHaveCount(0);
  expect(query(page).getAll('level')).toEqual(['error']);
  await page.getByRole('searchbox', { name: 'Search log lines' }).fill('upstream');
  await expect.poll(() => query(page).get('q')).toBe('upstream');
  await page.getByRole('button', { name: 'Pause live tail' }).click();
  expect(query(page).get('follow')).toBe('0');
  await page.reload();
  await expect(page.getByRole('button', { name: 'Resume live tail' })).toBeVisible();
  await expect(page.getByRole('checkbox', { name: /^Error/ })).toBeChecked();
  await expect(lines.getByText('ERROR request failed: upstream timeout token=[REDACTED]')).toBeVisible();
  await fits(page);
});

test('logs workspace: pod deep link, previous container and a second profile', async ({ page }) => {
  await page.goto('/applications/shop/resources?view=table');
  await page.getByRole('button', { name: 'Inspect Pod api-release-1 in piceli-test', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Resource details' });
  await dialog.getByRole('link', { name: 'Logs', exact: true }).click();
  await expect(page).toHaveURL(/\/logs\?scope=shop&pod=api-release-1$/);
  const lines = page.getByRole('region', { name: 'Container log lines' });
  await expect(lines.getByText('INFO listening on :8080')).toBeVisible();
  await expect(lines.getByText('level=info msg="listening" port=8080')).toHaveCount(0);
  const previous = page.getByRole('checkbox', { name: 'Previous container instance' });
  await previous.click();
  await expect(previous).toBeChecked();
  await expect(lines.getByText('ERROR previous instance exited: out of memory')).toBeVisible();
  await previous.click();
  await expect(previous).not.toBeChecked();
  await page.getByText('Add another profile').click();
  await page.getByRole('combobox', { name: 'Profile', exact: true }).selectOption('demo-west');
  await page.getByRole('textbox', { name: 'Namespace' }).fill('piceli-test');
  await page.getByRole('button', { name: 'Add scope' }).click();
  await expect.poll(() => query(page).getAll('profile')).toEqual(['demo-west:piceli-test']);
  const profileScope = page.getByRole('checkbox', { name: /demo-west · piceli-test/ });
  await expect(profileScope).toBeChecked();
  // The same pod name read through both profiles' own credentials.
  await expect(lines.getByText('INFO listening on :8080')).toHaveCount(2);
  // The source column names the scope (hidden on a phone, still in the row).
  await expect(lines.getByText(/^demo-west · piceli-test · api-release-1\/api$/).first()).toBeAttached();
  await fits(page);
});

test('forwards workspace: deep link, start, open, list and stop', async ({ page, request }, testInfo) => {
  const port = ports[testInfo.project.name];
  await page.goto('/applications/shop/resources?view=table');
  await page.getByRole('button', { name: 'Inspect Service api in piceli-test', exact: true }).click();
  await page.getByRole('dialog', { name: 'Resource details' }).getByRole('link', { name: 'Forward', exact: true }).click();
  await expect(page).toHaveURL(/\/forwards\?application=shop&resource=[0-9a-f]+&port=8080$/);
  const start = page.getByRole('region', { name: 'Start a forward' });
  await expect(start.getByRole('combobox', { name: 'Target', exact: true })).toHaveValue(query(page).get('resource'));
  await start.getByRole('spinbutton').fill(String(port));
  await start.getByRole('button', { name: 'Start forward' }).click();
  const active = page.getByRole('region', { name: 'Active forwards' });
  const link = active.getByRole('link', { name: `http://127.0.0.1:${port}` });
  await expect(link).toBeVisible();
  const fixture = await request.get(`http://127.0.0.1:${port}/`);
  expect(await fixture.text()).toContain('Fixture forward');
  const row = active.getByRole('listitem').filter({ hasText: `http://127.0.0.1:${port}` });
  await expect(row.getByText('Service/api')).toBeVisible();
  await expect(row.getByText('Shop · piceli-test · fake')).toBeVisible();
  await row.getByRole('button', { name: `Stop forward api ${port}` }).click();
  await expect(link).toHaveCount(0);
  await expect(page.getByText(/Recently ended/)).toBeVisible();
  // The listener is gone with its session.
  await expect.poll(async () => (await request.get(`http://127.0.0.1:${port}/`, { timeout: 2000 }).then(() => 'open', () => 'closed'))).toBe('closed');
  await fits(page);
});
