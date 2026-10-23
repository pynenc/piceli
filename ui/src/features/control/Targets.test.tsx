import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { App } from '../../app/App';

const product = 'a'.repeat(40);
const hosts = 'd'.repeat(40);
const document = {
  configured: true, schema: 'piceli.targets.v1', state: 'ok', hosts_read: false, generated_at: '2026-10-23T09:31:00Z',
  controller: { read: true, health: 'running', as_of: '2026-10-23T09:30:00Z' },
  targets: [
    { kind: 'environment', name: 'main', where: 'shop', state: 'deployed', as_of: '2026-10-23T09:30:00Z',
      revision: { commit: product, tag: null, commits: { product } }, pinned: { source: 'product', commit: product, commits: { product } },
      checks: { state: 'passed', passed: 3, total: 3, at: '2026-10-23T09:01:00Z' }, facts: { state: 'controller' } },
    { kind: 'nix-host', name: 'web-1', where: null, state: 'unknown', as_of: '2026-10-23T09:05:00Z',
      revision: { commit: hosts, tag: 'web-v1.2.0' }, pinned: { input: 'product', commit: product, environment: 'main', verified_at: '2026-10-23T09:00:00Z' },
      checks: { state: 'none' }, facts: { state: 'controller' }, release: { state: 'published', tag: 'web-v1.2.0', commit: hosts, at: '2026-10-23T09:05:00Z' } },
  ],
};

function open() {
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const url = new URL(request.url).pathname;
    if (url.endsWith('/capabilities')) return Response.json({ principal: { id: 'local', name: 'Local user' }, targets: [], actions: { composition: { allowed: true } } });
    if (url.endsWith('/composition/targets')) return Response.json(document);
    return Response.json({});
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/composition/targets']}><App /></MemoryRouter></QueryClientProvider>);
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('lists every environment and NixHost with its pinned product commit', async () => {
  open();
  const table = await screen.findByRole('region', { name: 'Targets' });
  const main = within(table).getByRole('row', { name: /Target main shop/ });
  expect(within(main).getAllByText('product@aaaaaaa').length).toBe(1);
  expect(within(main).getByText('3/3', { exact: false })).toBeTruthy();
  const web = within(table).getByRole('row', { name: /Target web-1/ });
  expect(within(web).getByText('web-v1.2.0', { exact: false })).toBeTruthy();
  expect(within(web).getByText(/verified in main/)).toBeTruthy();
  expect(within(web).getByText(/Release published/)).toBeTruthy();
  // The in-cluster UI says it shows the controller's view of each host.
  expect(screen.getByText('Hosts are not reached from this UI')).toBeTruthy();
});
