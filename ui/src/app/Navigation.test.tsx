import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { Capabilities, NavigationSummary } from '../api/generated';
import { Navigation } from './Navigation';

const allowed = { allowed: true };
const capabilities: Capabilities = { principal: { id: 'local', name: 'Local' }, targets: [], actions: { composition: allowed, composition_history: allowed, cluster_status: allowed, logs: allowed, access: allowed } };
const summary: NavigationSummary = { environments: [{ name: 'main', state: 'deployed', health: 'healthy', application_id: 'env-1' }, { name: 'preview', state: 'approval-required', health: 'unknown' }], approvals: 1, degraded_environments: 2, failed_builds: 1, stale_forwards: 3, registry_warnings: 1 };
beforeEach(() => {
  window.localStorage.clear();
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => new URL(request.url).pathname.endsWith('/navigation') ? Response.json(summary) : Response.json({})));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function open(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><Navigation capabilities={capabilities} /></MemoryRouter></QueryClientProvider>);
}

it('shows alert counts beside the pages that resolve them', async () => {
  open('/applications');
  const nav = screen.getByRole('navigation', { name: 'Main navigation' });
  expect(await within(nav).findByTitle('1 approvals waiting')).toBeTruthy();
  expect(within(nav).getByTitle('2 degraded environments')).toBeTruthy();
  expect(within(nav).getByTitle('3 stale forwards')).toBeTruthy();
  expect(within(nav).getByTitle('1 registry warnings')).toBeTruthy();
  expect(within(nav).getByTitle('1 failed builds')).toBeTruthy();
  expect(within(nav).getByRole('link', { name: 'Logs' }).getAttribute('href')).toBe('/logs');
  expect(within(nav).getByRole('link', { name: 'Port forwards' }).getAttribute('href')).toBe('/forwards');
  expect(within(nav).getByRole('link', { name: 'Deployment history' })).toBeTruthy();
});

it('opens the current sub-menu and remembers an explicit choice', async () => {
  const view = open('/composition/environments/main');
  const toggle = await screen.findByRole('button', { name: 'Collapse Environments' });
  expect(toggle.getAttribute('aria-expanded')).toBe('true');
  expect(await screen.findByRole('link', { name: 'preview' })).toBeTruthy();
  const user = userEvent.setup();
  await user.click(toggle);
  expect(screen.getByRole('button', { name: 'Expand Environments' }).getAttribute('aria-expanded')).toBe('false');
  expect(screen.queryByRole('link', { name: 'preview' })).toBeNull();
  expect(JSON.parse(window.localStorage.getItem('piceli.nav.open')!)).toEqual({ '/composition': false });
  const cluster = screen.getByRole('button', { name: 'Expand Cluster' });
  cluster.focus();
  await user.keyboard('{Enter}');
  expect(screen.getByRole('link', { name: 'Registry' }).getAttribute('href')).toBe('/cluster#registry');
  view.unmount();
  open('/composition/environments/main');
  expect(await screen.findByRole('button', { name: 'Expand Environments' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Collapse Cluster' })).toBeTruthy();
});
