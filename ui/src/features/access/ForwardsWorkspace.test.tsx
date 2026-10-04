import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { Capabilities, ForwardPage, Resource } from '../../api/generated';
import { ForwardsWorkspace } from './ForwardsWorkspace';

const capabilities: Capabilities = { principal: { id: 'local', name: 'Local' }, targets: [], actions: { access: { allowed: true } } };
const identity = { target_id: 't', api_version: 'v1', kind: 'Service', namespace: 'shop', name: 'api', uid: 'svc-uid' };
const service: Resource = { id: 'svc', identity, presence: 'present', ownership: 'unknown', ports: [8080, 9090], capabilities: { access: { allowed: true } } };
let page: ForwardPage;
let posts: { path: string; body: unknown }[];
beforeEach(() => {
  posts = [];
  page = {
    mode: 'local', orphans: 1,
    items: [
      { session: { id: 's1', application_id: 'shop', resource: identity, principal_id: 'local', binding_location: 'server', state: 'ready', endpoint: '127.0.0.1:18080', expires_at: '2026-10-03T11:00:00Z', local_port: 18080, remote_port: 8080 }, application_name: 'Shop', scope: { id: 'shop', name: 'Shop', kind: 'application', cluster: 'east', namespace: 'shop' }, url: 'http://127.0.0.1:18080' },
      { session: { id: 's2', application_id: 'env-gone', resource: identity, principal_id: 'local', binding_location: 'server', state: 'ready', endpoint: '127.0.0.1:18081', expires_at: '2026-10-03T11:00:00Z', local_port: 18081, remote_port: 8080 }, application_name: 'Removed scope', stale: true, stale_reason: 'scope-removed' },
    ],
  };
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const url = new URL(request.url);
    if (request.method !== 'GET') { posts.push({ path: url.pathname, body: request.method === 'POST' ? await request.json().catch(() => null) : null }); return Response.json(url.pathname.endsWith('/stale/stop') ? { stopped: 2, orphans: 0 } : { ...page.items[0].session, state: request.method === 'DELETE' ? 'stopped' : 'ready' }, { status: url.pathname.endsWith('/access-sessions') ? 201 : 200 }); }
    if (url.pathname.endsWith('/forwards')) return Response.json(page);
    if (url.pathname.endsWith('/applications')) return Response.json({ items: [{ id: 'shop', name: 'Shop', target: { id: 't', name: 'east', namespace: 'shop' }, definition_kind: 'inventory', ownership: 'inventory', freshness: { state: 'connected' } }], cursor: 'c' });
    if (url.pathname.endsWith('/resources')) return Response.json({ items: [service], cursor: 'c', freshness: { state: 'connected' } });
    return Response.json({});
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function open(path: string, caps = capabilities) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><ForwardsWorkspace capabilities={caps} /></MemoryRouter></QueryClientProvider>);
}

it('lists forwards across scopes with their local address and tidies stale ones', async () => {
  open('/forwards');
  const active = await screen.findByRole('region', { name: 'Active forwards' });
  expect(within(active).getByRole('link', { name: 'http://127.0.0.1:18080' }).getAttribute('href')).toBe('http://127.0.0.1:18080');
  expect(within(active).getByText(/Its scope was removed/)).toBeTruthy();
  expect(screen.getByText('2 stale forwards')).toBeTruthy();
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: 'Stop stale forwards' }));
  await waitFor(() => expect(posts.map(item => item.path)).toContain('/api/v1/forwards/stale/stop'));
  await user.click(within(active).getByRole('button', { name: 'Stop forward api 18080' }));
  await waitFor(() => expect(posts.map(item => item.path)).toContain('/api/v1/applications/shop/access-sessions/s1'));
  expect(within(active).queryByRole('button', { name: 'Stop forward api 18081' })).toBeNull();
});

it('starts a forward for a deep-linked target with its port preselected', async () => {
  open('/forwards?application=shop&resource=svc&port=9090');
  const start = await screen.findByRole('region', { name: 'Start a forward' });
  await waitFor(() => expect((within(start).getByRole('combobox', { name: 'Target port' }) as HTMLSelectElement).value).toBe('9090'));
  expect((within(start).getByRole('spinbutton') as HTMLInputElement).value).toBe('9090');
  await userEvent.setup().click(within(start).getByRole('button', { name: 'Start forward' }));
  await waitFor(() => expect(posts.find(item => item.path.endsWith('/access-sessions'))?.body).toEqual({ resource_id: 'svc', resource_uid: 'svc-uid', remote_port: 9090, local_port: 9090, duration_seconds: 900 }));
});

it('explains that the in-cluster UI starts forwards on your machine', async () => {
  page = { mode: 'unavailable', reason: 'forwards-not-configured', items: [] };
  open('/forwards', { ...capabilities, actions: { composition: { allowed: true } } });
  expect(await screen.findByText('Forwards start on your machine')).toBeTruthy();
  expect(screen.queryByRole('region', { name: 'Start a forward' })).toBeNull();
});
