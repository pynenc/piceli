import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { App } from './App';

function open(path: string, canReadEnvironments = false) {
  const application = { id: 'shop', name: 'Shop', target: { id: 'target-shop', name: 'test-cluster', namespace: 'shop' }, definition_kind: 'inventory', ownership: 'inventory', freshness: { state: 'connected' }, capabilities: { evaluate: { allowed: true }, activity: { allowed: true } } };
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const route = new URL(request.url).pathname;
    if (route.endsWith('/capabilities')) return Response.json({ mode: 'cluster', principal: { id: 'viewer', name: 'Viewer' }, targets: [application.target], actions: { composition: { allowed: true }, gitops: { allowed: true }, environments: { allowed: canReadEnvironments } } });
    if (route.endsWith('/applications/shop')) return Response.json(application);
    if (route.endsWith('/resources')) return Response.json({ items: [], cursor: 'resources', freshness: application.freshness, partial: [] });
    if (route.endsWith('/operations')) return Response.json({ items: [], cursor: 'operations' });
    if (route.endsWith('/actions')) return Response.json({ promote: { allowed: false }, approve: { allowed: false }, wake: { allowed: false }, refs: [], summary: { namespace: null, revision: {}, components: [] } });
    if (route.includes('/composition/environments/')) return Response.json({ configured: true, controller: null, sources: [], environment: { name: 'stage/review', namespace: null, state: 'pending', health: 'unknown', revision: {}, components: [] } });
    if (route.endsWith('/gitops')) return Response.json({ configured: false, envs: [] });
    if (route.endsWith('/profiles')) return Response.json({ active: null, profiles: [] });
    return Response.json({ code: 'not-found', message: 'Outside this test scope.', correlation_id: 'test' }, { status: 404 });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>);
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each([['overview', 'Overview'], ['resources', 'Resources'], ['changes', 'Changes'], ['activity', 'Activity']])('shows the current application %s view in an accessible breadcrumb', async (tab, label) => {
  open(`/applications/shop/${tab}`);
  await screen.findByRole('heading', { name: 'Shop' });
  const breadcrumb = screen.getByRole('navigation', { name: 'Breadcrumb' });
  expect(within(breadcrumb).getByRole('link', { name: 'Applications' }).getAttribute('href')).toBe('/applications');
  expect(within(breadcrumb).getByText(label).getAttribute('aria-current')).toBe('page');
});

it('shows the decoded named environment and its inventory return link', async () => {
  open('/composition/environments/stage%2Freview');
  await screen.findByRole('heading', { name: 'stage/review' });
  const breadcrumb = screen.getByRole('navigation', { name: 'Breadcrumb' });
  expect(within(breadcrumb).getByRole('link', { name: 'Environments' }).getAttribute('href')).toBe('/composition');
  expect(within(breadcrumb).getByText('stage/review').getAttribute('aria-current')).toBe('page');
});

it('does not offer a branch environments link when that session can only read GitOps', async () => {
  open('/gitops');
  await screen.findByText('Controller not found');
  expect(screen.queryByRole('link', { name: 'View branch environments' })).toBeNull();
});

it('keeps the branch environments return link when that read capability is available', async () => {
  open('/gitops', true);
  await screen.findByText('Controller not found');
  expect(screen.getByRole('link', { name: 'View branch environments' }).getAttribute('href')).toBe('/environments');
});
