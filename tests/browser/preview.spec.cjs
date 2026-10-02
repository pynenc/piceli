// Deliberately use fresh browser sessions without the launch-token test helper.
const { test, expect } = require('./runtime.cjs');

test('a plain preview deep link opens in a fresh browser and survives reload', async ({ page, request }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  expect((await request.get('/api/v1/capabilities')).status()).toBe(403);
  await page.goto('/composition/overview?environment=main&component=web');
  await expect(page.getByRole('heading', { name: 'Your delivery landscape' })).toBeVisible();
  const selected = page.getByRole('complementary', { name: 'Selected component' });
  await expect(selected.getByRole('heading', { name: 'web', exact: true })).toBeVisible();
  const url = new URL(page.url());
  expect(url.searchParams.get('environment')).toBe('main');
  expect(url.searchParams.get('component')).toBe('web');
  expect(url.searchParams.has('token')).toBe(false);
  await page.reload();
  await expect(selected.getByRole('heading', { name: 'web', exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test('a localhost bookmark with an old preview token recovers without copying a token', async ({ page, baseURL }) => {
  const url = new URL('/applications?q=Shop&layout=table', baseURL);
  url.hostname = 'localhost';
  url.searchParams.set('token', 'expired-preview-token-from-an-older-run');
  await page.goto(url.href);
  await expect(page.getByRole('heading', { name: 'Applications', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Shop', exact: true })).toBeVisible();
  await expect(page.getByRole('searchbox', { name: 'Search applications' })).toHaveValue('Shop');
  await expect(page.getByRole('button', { name: 'Table', exact: true })).toHaveAttribute('aria-pressed', 'true');
  expect(new URL(page.url()).hostname).toBe('127.0.0.1');
  expect(new URL(page.url()).searchParams.has('token')).toBe(false);
  await page.reload();
  await expect(page.getByRole('link', { name: 'Shop', exact: true })).toBeVisible();
});

test('a link from another site opens the preview in a fresh browser', async ({ page, baseURL }) => {
  const link = new URL('/composition/overview?token=expired-preview-token-from-an-older-run', baseURL);
  await page.route('http://preview-links.example/', route => route.fulfill({
    contentType: 'text/html',
    body: `<a href="${link.href}">Open Piceli</a>`,
  }));
  await page.goto('http://preview-links.example/');
  await page.getByRole('link', { name: 'Open Piceli' }).click();
  await expect(page.getByRole('heading', { name: 'Your delivery landscape' })).toBeVisible();
  expect(new URL(page.url()).searchParams.has('token')).toBe(false);
});
