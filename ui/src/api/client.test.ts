import { afterEach, expect, it, vi } from 'vitest';
import { ApiError, api } from './client';
afterEach(() => { document.querySelector('base')?.remove(); vi.unstubAllGlobals(); });
it('uses the server prefix and same-origin credentials for deep routes', async () => {
  const base = document.createElement('base'); base.href = '/piceli/'; document.head.append(base);
  const fetch = vi.fn().mockResolvedValue(Response.json({ items: [] })); vi.stubGlobal('fetch', fetch);
  await api.applications(null);
  const request = fetch.mock.calls[0][0] as Request;
  expect(new URL(request.url).pathname).toBe('/piceli/api/v1/applications');
  expect(request.credentials).toBe('same-origin');
});
it('does not expose non-JSON proxy errors in the UI', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('private path /secret', { status: 502 })));
  try { await api.applications(null); throw new Error('request should fail'); } catch (error) { expect(error).toBeInstanceOf(ApiError); expect((error as Error).message).not.toContain('/secret'); }
});
