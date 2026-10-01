const { test, expect } = require('./runtime.cjs');

test('signed OIDC login continues with a strict cookie and revocable scope', async ({ browser }, testInfo) => {
  const origin = testInfo.project.use.baseURL;
  const context = await browser.newContext({ baseURL: origin, ignoreHTTPSErrors: true });
  try {
    const page = await context.newPage();
    const navigations = [];
    page.on('request', request => {
      if (request.isNavigationRequest()) navigations.push(request.url());
    });

    await page.goto('/applications');
    await expect(page).toHaveURL(`${origin}/applications`);
    await expect(page.getByRole('heading', { name: 'Applications', exact: true })).toBeVisible();
    expect(navigations.some(url => url.startsWith('http://127.0.0.1:') && url.includes('/authorize?'))).toBe(true);
    expect(navigations.some(url => url.startsWith(`${origin}/auth/callback?code=`))).toBe(true);
    expect(navigations.at(-1)).toBe(`${origin}/applications`);

    const cookies = await context.cookies(origin);
    const session = cookies.find(cookie => cookie.name.startsWith('piceli_cluster_session_'));
    const csrf = cookies.find(cookie => cookie.name.startsWith('piceli_csrf_'));
    expect(session).toMatchObject({ httpOnly: true, secure: true, sameSite: 'Strict' });
    expect(csrf).toMatchObject({ httpOnly: false, secure: true, sameSite: 'Strict' });

    const before = await page.evaluate(async () => {
      const [applications, resources, other] = await Promise.all([
        fetch('/api/v1/applications'),
        fetch('/api/v1/applications/shop/resources'),
        fetch('/api/v1/applications/other'),
      ]);
      return {
        applications: (await applications.json()).items.map(item => item.id),
        resources: (await resources.json()).items.map(item => item.identity.name),
        other: other.status,
      };
    });
    expect(before).toEqual({ applications: ['shop'], resources: ['web'], other: 404 });

    const revoke = await page.evaluate(async csrfCookie => {
      const response = await fetch('/__fixture/revoke', {
        method: 'POST',
        headers: { 'X-Piceli-CSRF': csrfCookie },
      });
      return response.status;
    }, csrf.value);
    expect(revoke).toBe(204);

    const after = await page.evaluate(async () => {
      const [applications, resources] = await Promise.all([
        fetch('/api/v1/applications'),
        fetch('/api/v1/applications/shop/resources'),
      ]);
      return {
        applications: (await applications.json()).items.map(item => item.id),
        resources: resources.status,
      };
    });
    expect(after).toEqual({ applications: [], resources: 404 });
  } finally {
    await context.close();
  }
});
