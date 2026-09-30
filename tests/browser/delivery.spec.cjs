const { test, expect } = require('./runtime.cjs');
const net = require('node:net');

async function unusedLoopbackPort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}

// Every request reaches the local authenticated service; no Playwright routes.
async function request(page, path, body) {
  return page.evaluate(async ({ path, body }) => {
    const token = document.cookie.split('; ').find(value => value.startsWith('piceli_csrf='))?.slice('piceli_csrf='.length);
    const response = await fetch(`/api/v1/${path}`, body === undefined ? {} : {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Piceli-CSRF': decodeURIComponent(token || '') },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(`${path}: ${response.status} ${JSON.stringify(result)}`);
    return result;
  }, { path, body });
}

async function plan(page, app, { release, changed = false } = {}) {
  console.log(`Starting ${release ? 'rollback' : 'deployment'} review for ${app}`);
  await page.goto(`/applications/${app}/changes${release ? '?intent=rollback' : ''}`);
  console.log(`Loaded review page for ${app}`);
  if (release) await page.getByRole('combobox', { name: 'Archived release', exact: true }).selectOption(release);
  console.log(`Selected review source for ${app}`);
  await page.getByRole('button', { name: 'Prepare source preview', exact: true }).click();
  try {
    await expect(page.getByRole('heading', { name: 'Review source evaluation', exact: true })).toBeVisible();
  } catch (error) {
    console.log('Preview page state:', (await page.locator('body').innerText()).slice(0, 1800));
    throw error;
  }
  await page.getByText('Included files (1)', { exact: true }).click();
  await expect(page.getByText('composition.py', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Approve source evaluation', exact: true }).click();
  await expect(page.getByRole('heading', { name: release ? 'Review rollback' : 'Review deployment', exact: true })).toBeVisible({ timeout: 90000 });
  await expect(page.getByRole('heading', { name: 'Field changes', exact: true })).toBeVisible();
  if (changed) {
    await expect(page.getByText(`Deployment / ${app}`, { exact: true })).toBeVisible();
    await expect(page.getByLabel('Before /spec/replicas', { exact: true })).toBeVisible();
    await expect(page.getByLabel('After /spec/replicas', { exact: true })).toBeVisible();
  }
  const deploy = page.getByRole('button', { name: release ? /^Roll back to / : /^Deploy to / });
  await expect(deploy).toBeDisabled();
  const confirmation = page.getByRole('checkbox', { name: /^I reviewed the changes, source revision and target / });
  await expect(confirmation).not.toBeChecked();
  await confirmation.check();
  await expect(deploy).toBeEnabled();
  const id = new URL(page.url()).searchParams.get('plan');
  expect(id).toBeTruthy();
  const record = await request(page, `plans/${id}`);
  expect(record.application_id).toBe(app);
  expect(record.intent).toBe(release ? 'rollback' : 'deploy');
  if (changed) expect(record.diffs.length).toBeGreaterThan(0);
  else expect(record.summary.create).toBeGreaterThan(0);
  await deploy.click();
  await expect(page).toHaveURL(/\/runs\/[^/?]+$/);
  return { id: page.url().split('/').at(-1), plan: record };
}

async function completed(page, id, state = 'succeeded') {
  console.log(`Waiting for ${id} to become ${state}`);
  await expect.poll(async () => (await request(page, `operations/${id}`)).state, { timeout: 300000, intervals: [500, 1000] }).toBe(state);
  const operation = await request(page, `operations/${id}`);
  console.log(`${id} is ${operation.state}`);
  await expect(page.getByRole('heading', { name: 'Deployment run', exact: true })).toBeVisible();
  await expect(page.locator('.detail-heading .badge')).toHaveText(new RegExp(state, 'i'));
  expect(operation.stages.length).toBeGreaterThan(0);
  if (state === 'succeeded') {
    expect(operation.deployment_outcome).toBe('succeeded');
    expect(operation.engine_execution_id).toBeTruthy();
    expect(operation.receipts.length).toBeGreaterThan(0);
    await page.getByText(/^Receipts \(/).click();
    await expect(page.getByLabel('Execution receipts', { exact: true })).toBeVisible();
  }
  return operation;
}

async function observed(page, app, version) {
  await expect.poll(async () => {
    const actual = await request(page, `__fixture/${app}/observe`, {});
    return { replicas: actual.replicas, ready: actual.ready, version: actual.version };
  }, { timeout: 60000 }).toEqual({ replicas: version, ready: version, version: String(version) });
  const actual = await request(page, `__fixture/${app}/observe`, {});
  expect(actual.image).toBe('docker.io/library/nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10');
}

test('approved delivery, durable reconnect, source change, archived rollback and recovery', async ({ page, context }, testInfo) => {
  const app = `shop-${testInfo.project.name}`;
  const fake = !process.env.PICELI_KIND_KUBECONFIG;
  await page.goto(`/applications/${app}/changes`);
  // Hold fake readiness so the first browser disconnect happens during work.
  if (fake) await request(page, `__fixture/${app}/readiness`, { ready: false });
  const first = await plan(page, app);
  const durableUrl = page.url();
  if (fake) {
    await expect.poll(async () => (await request(page, `operations/${first.id}`)).state).toBe('running');
  }
  await page.close();
  page = await context.newPage();
  await page.goto(durableUrl);
  await expect(page.getByRole('heading', { name: 'Deployment run', exact: true })).toBeVisible();
  if (fake) await request(page, `__fixture/${app}/readiness`, { ready: true });
  const firstOperation = await completed(page, first.id);
  expect(firstOperation.plan_id).toBe(first.plan.id);
  expect(firstOperation.approved_digest).toBe(first.plan.digest);
  await observed(page, app, 1);

  if (!fake) {
    await page.goto(`/applications/${app}/resources`);
    await page.getByRole('button', { name: new RegExp(`Inspect Deployment ${app} in `) }).click();
    await page.getByRole('button', { name: 'Access', exact: true }).click();
    const port = await unusedLoopbackPort();
    await page.getByRole('spinbutton', { name: 'Local port' }).fill(String(port));
    await page.getByRole('button', { name: 'Start local connection' }).click();
    await expect(page.getByText(`127.0.0.1:${port}`, { exact: true })).toBeVisible({ timeout: 30000 });
    const throughForward = await context.request.get(`http://127.0.0.1:${port}/`);
    expect(throughForward.status()).toBe(200);
    await page.getByRole('button', { name: 'Logs', exact: true }).click();
    await expect(page.getByRole('combobox', { name: 'Pod' })).toBeVisible({ timeout: 30000 });
    await expect(page.getByRole('combobox', { name: 'Container' })).toHaveValue('api');
    await expect(page.getByLabel('Container log lines')).toContainText('GET / HTTP/1.1', { timeout: 30000 });
    await page.getByRole('combobox', { name: 'Container' }).selectOption('logger');
    await expect(page.getByLabel('Container log lines')).toContainText('piceli-current-marker', { timeout: 30000 });
    await page.getByRole('combobox', { name: 'Instance' }).selectOption('previous');
    await expect(page.getByLabel('Container log lines')).toContainText('piceli-previous-marker', { timeout: 30000 });
    await page.getByRole('button', { name: 'Access', exact: true }).click();
    await page.getByRole('button', { name: 'Stop connection' }).click();
    await expect(page.getByRole('button', { name: 'Stop connection' })).toHaveCount(0);
    await page.getByRole('button', { name: 'Close resource inspector' }).click();
    const tableCount = await page.locator('.resource-row').count();
    await page.getByRole('button', { name: 'Relationships' }).click();
    await expect(page.locator('.resource-row')).toHaveCount(tableCount);
    await expect(page.getByRole('button', { name: /Inspect ReplicaSet / })).toBeVisible();
    await page.getByRole('button', { name: /Inspect Pod / }).first().click();
    await expect(page.getByRole('heading', { name: 'Resource details' })).toBeVisible();
    await page.getByRole('button', { name: 'Close resource inspector' }).click();
    await page.getByRole('button', { name: 'Table', exact: true }).click();
    await expect(page.locator('.resource-row')).toHaveCount(tableCount);
  }

  const observer = await context.newPage();
  const eventResponse = observer.waitForResponse(response => response.url().includes('/api/v1/events?') && response.status() === 200);
  await observer.goto(`/applications/${app}/activity`);
  await eventResponse;
  await expect(observer.getByText('Live updates disconnected', { exact: true })).toBeHidden();

  let resourceObserver;
  if (!fake) {
    resourceObserver = await context.newPage();
    await resourceObserver.goto(`/applications/${app}/resources`);
    const snapshot = await request(resourceObserver, `applications/${app}/resources`);
    expect(snapshot.event_cursor).toBeDefined();
    await resourceObserver.evaluate(({ app, cursor }) => {
      window.__observationEvents = [];
      const source = new EventSource(`/api/v1/observation-events?application_id=${encodeURIComponent(app)}&after=${cursor}`);
      source.addEventListener('upsert', event => window.__observationEvents.push(JSON.parse(event.data)));
      window.__observationSource = source;
    }, { app, cursor: snapshot.event_cursor });
  }

  await request(page, `__fixture/${app}/source`, { version: 2 });
  const second = await plan(page, app, { changed: true });
  expect(second.plan.source.revision).not.toBe(first.plan.source.revision);
  expect(second.plan.digest).not.toBe(first.plan.digest);
  await completed(page, second.id);
  await observed(page, app, 2);
  if (resourceObserver) {
    await expect.poll(() => resourceObserver.evaluate(app => window.__observationEvents.some(event => event.subject_id === `apps/v1/Deployment/${app}`), app), { timeout: 15000 }).toBe(true);
    await resourceObserver.evaluate(() => window.__observationSource.close());
    await resourceObserver.close();
  }
  await expect(observer.locator('.operation-row')).toHaveCount(2, { timeout: 10000 });
  await observer.close();
  console.log(`Observed second deployment for ${app}`);

  const archives = await request(page, `applications/${app}/releases`);
  console.log(`Archived releases for ${app}: ${archives.items.map(item => item.name).join(', ')}`);
  expect(archives.items.some(item => item.name === firstOperation.engine_release)).toBe(true);
  const rollback = await plan(page, app, { release: firstOperation.engine_release, changed: true });
  expect(rollback.plan.release).toBe(firstOperation.engine_release);
  await completed(page, rollback.id);
  await observed(page, app, 1);

  if (fake) {
    await request(page, `__fixture/${app}/source`, { version: 3 });
    await request(page, `__fixture/${app}/readiness`, { ready: false });
    const failing = await plan(page, app, { changed: true });
    const failed = await completed(page, failing.id, 'failed');
    await expect(page.getByText('Deployment failed', { exact: true })).toBeVisible();
    expect(failed.capabilities.resume.allowed).toBe(true);
    await request(page, `__fixture/${app}/readiness`, { ready: true });
    await page.getByRole('button', { name: 'Resume as a new attempt', exact: true }).click();
    await expect(page.getByRole('dialog')).toContainText('Resume this exact plan?');
    await page.getByRole('button', { name: 'Approve new attempt', exact: true }).click();
    await expect(page).not.toHaveURL(new RegExp(`/runs/${failing.id}$`));
    const resumedId = page.url().split('/').at(-1);
    const recovered = await completed(page, resumedId);
    expect(recovered.recovery_of).toBe(failing.id);
    expect(recovered.plan_id).toBe(failing.plan.id);
    expect(recovered.approved_digest).toBe(failing.plan.digest);
    expect(recovered.attempt).toBe(failed.attempt + 1);
    expect((await request(page, `operations/${failing.id}`)).state).toBe('failed');
    await observed(page, app, 3);
  }
  // This is viewport layout evidence, independent of operation status assertions.
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
