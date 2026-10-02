import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { App } from '../../app/App';
import { CompositionEnvironment, shortDigest, shortSha } from './Composition';

const sha = '3f9c2d1e8a7b4c5d6e0f1a2b3c4d5e6f7a8b9c0d';
const environment = {
  name: 'main', namespace: 'shop', state: 'deployed', health: 'healthy',
  revision: { product: sha, assets: 'b'.repeat(40) }, last_sync: '2026-10-01T09:01:00Z',
  plan_hash: null, application_id: 'env-main',
  components: [
    { name: 'api', source: 'product', commit: sha, digest: `sha256:${'a'.repeat(64)}`, state: 'synced', health: 'healthy', updated_at: '2026-10-01T09:01:00Z' },
    { name: 'worker', source: 'product', commit: null, digest: null, state: 'building', health: 'unknown' },
  ],
};
const overview = {
  configured: true, controller: { state: 'running', last_poll: '2026-10-01T09:30:00Z' },
  sources: [{ name: 'product', url: 'https://git.example/shop/product.git', refs: { main: sha }, last_poll: '2026-10-01T09:30:00Z' }],
  environments: [environment, { ...environment, name: 'wp-login', namespace: null, state: 'approval-required', plan_hash: `sha256:${'d'.repeat(64)}`, application_id: null, components: [] }],
};

function open(path: string, { sync = true, configured = true, refuse = false } = {}) {
  const posts: unknown[] = [];
  document.cookie = 'piceli_csrf_this=test-csrf; path=/';
  const meta = document.createElement('meta'); meta.name = 'piceli-csrf-cookie'; meta.content = 'piceli_csrf_this'; document.head.append(meta);
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const url = new URL(request.url).pathname;
    if (request.method === 'POST') {
      const body = JSON.parse(await request.text());
      posts.push(body);
      if (refuse) return Response.json({ code: 'ui-sync-target-unknown', message: "The controller's status does not list that environment or component. Refresh and try again.", correlation_id: 'c1' }, { status: 404 });
      return Response.json({ state: 'requested', env: body.env, component: body.component, request: 'sync.1' }, { status: 202 });
    }
    if (url.endsWith('/capabilities')) return Response.json({ principal: { id: 'local', name: 'Local user' }, targets: [], actions: { composition: { allowed: true }, composition_sync: { allowed: sync } } });
    if (url.endsWith('/composition')) return Response.json(configured ? overview : { configured: false, controller: null, sources: [], environments: [] });
    if (url.endsWith('/composition/environments/main')) return Response.json({ configured: true, controller: overview.controller, sources: overview.sources, environment });
    return Response.json({});
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>);
  return posts;
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); document.cookie = 'piceli_csrf_this=; Max-Age=0; path=/'; document.querySelector('meta[name="piceli-csrf-cookie"]')?.remove(); });

it('opens the environment inventory with revisions per source and sync', async () => {
  const posts = open('/composition');
  const card = await screen.findByRole('region', { name: 'Environment main' });
  expect(within(card).getByText('3f9c2d1')).toBeTruthy();
  expect(within(card).getByText('product')).toBeTruthy();
  expect(within(card).getByText('1 synced · 1 building')).toBeTruthy();
  const pending = screen.getByRole('region', { name: 'Environment wp-login' });
  expect(within(pending).getByText(/piceli gitops approve wp-login sha256:d+/)).toBeTruthy();
  await userEvent.setup().click(within(card).getByRole('button', { name: 'Sync main' }));
  expect(await within(card).findByText(/Sync requested/)).toBeTruthy();
  expect(posts).toEqual([{ env: 'main', component: null }]);
});

it('opens the infrastructure graph at the root and keeps the environment inventory one click away', async () => {
  open('/');
  expect(await screen.findByRole('region', { name: 'Infrastructure topology' })).toBeTruthy();
  const nav = screen.getByRole('navigation', { name: 'Main navigation' });
  const environments = within(nav).getByRole('link', { name: 'Environments' });
  expect(environments.getAttribute('href')).toBe('/composition');
  await userEvent.setup().click(environments);
  expect(await screen.findByRole('heading', { name: 'Environments' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Sync main' })).toBeTruthy();
});

it('shows components, syncs one, and links the workloads and logs', async () => {
  const posts = open('/composition/environments/main');
  expect(await screen.findByRole('heading', { name: 'main' })).toBeTruthy();
  const components = screen.getByRole('region', { name: 'Components' });
  expect(within(components).getByText('api')).toBeTruthy();
  expect(within(components).getByText('Synced')).toBeTruthy();
  expect(within(components).getByText('not built')).toBeTruthy();
  await userEvent.setup().click(within(components).getByRole('button', { name: 'Sync worker' }));
  expect(posts).toEqual([{ env: 'main', component: 'worker' }]);
  const link = screen.getByRole('link', { name: 'Open workloads, pods and logs' }) as HTMLAnchorElement;
  expect(link.getAttribute('href')).toBe('/applications/env-main/resources');
});

it('shows a friendly refusal when the sync target is unknown', async () => {
  open('/composition', { refuse: true });
  const card = await screen.findByRole('region', { name: 'Environment main' });
  await userEvent.setup().click(within(card).getByRole('button', { name: 'Sync main' }));
  expect(await screen.findByText(/does not list that environment or component/)).toBeTruthy();
});

it('hides sync without the grant and explains an absent controller', async () => {
  open('/composition', { sync: false });
  await screen.findByRole('region', { name: 'Environment main' });
  expect(screen.queryByRole('button', { name: /^Sync/ })).toBeNull();
  cleanup(); vi.unstubAllGlobals();
  open('/composition', { configured: false });
  expect(await screen.findByText('No GitOps controller status yet')).toBeTruthy();
});

it('lists sources with their refs', async () => {
  open('/composition/sources');
  const source = await screen.findByRole('region', { name: 'Source product' });
  expect(within(source).getByText('https://git.example/shop/product.git')).toBeTruthy();
  const refs = within(source).getByRole('region', { name: 'Refs for product' });
  expect(within(refs).getByText('main')).toBeTruthy();
  expect(within(refs).getByText('3f9c2d1')).toBeTruthy();
});

it('shortens commits and digests', () => {
  expect(shortSha(sha)).toBe('3f9c2d1');
  expect(shortSha(null)).toBe('unknown');
  expect(shortDigest(`sha256:${'a'.repeat(64)}`)).toBe(`sha256:${'a'.repeat(12)}…`);
});

it('renders the header actions slot before Sync', async () => {
  open('/unused');
  cleanup();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/composition/environments/main']}><Routes><Route path="/composition/environments/:env" element={<CompositionEnvironment canSync actions={env => <button>Promote {env.name}</button>} />} /></Routes></MemoryRouter></QueryClientProvider>);
  const promote = await screen.findByRole('button', { name: 'Promote main' });
  const sync = screen.getByRole('button', { name: 'Sync main' });
  expect(promote.compareDocumentPosition(sync) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});
