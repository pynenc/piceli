import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { App } from './App';

const target = { id: 'target', name: 'Cluster A', namespace: 'shop' };
const app = { id: 'shop', name: 'Shop', target, definition_kind: 'inventory', ownership: 'native', freshness: { state: 'connected' }, capabilities: { activity: { allowed: true }, evaluate: { allowed: false } } };
const apps = [app, { ...app, id: 'tools', name: 'Tools', target: { ...target, namespace: 'tools' } }, { ...app, id: 'hidden', name: 'Hidden history', capabilities: { activity: { allowed: false } } }];
function Address() { const location = useLocation(); return <output aria-label="Address">{location.pathname}{location.search}</output>; }
function open(path: string, allowed = true) {
  const requested: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const route = new URL(request.url).pathname; requested.push(route);
    if (route.endsWith('/capabilities')) return Response.json({ mode: 'cluster', principal: { id: 'reader', name: 'Reader' }, targets: [target], actions: { activity: { allowed }, cluster_status: { allowed: true } } });
    if (route.endsWith('/applications')) return Response.json({ items: apps, cursor: 'apps' });
    if (route.endsWith('/applications/shop')) return Response.json(app);
    if (route.endsWith('/plans')) return Response.json({ items: [], next_page: null });
    if (route.endsWith('/operations')) return Response.json({ items: [], cursor: '0' });
    if (route.endsWith('/resources')) return Response.json({ items: [], freshness: app.freshness, cursor: '0', partial: [] });
    if (route.endsWith('/cluster/status')) return Response.json({ cluster: 'Cluster A', state: 'ready', nodes: [{ name: 'worker-a', arch: 'arm64', roles: ['worker'], ready: true, mirror: null }], controller: null, registry: null, ui: null });
    return Response.json({ code: 'not-found', message: 'Not in this fixture', correlation_id: 'test' }, { status: 404 });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App /><Address /></MemoryRouter></QueryClientProvider>);
  return requested;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('visible deployment history navigation', () => {
  it('keeps the existing cluster page next to workspace navigation and preserves node evidence', async () => {
    open('/cluster');
    await screen.findByRole('heading', { name: 'Cluster' });
    const link = within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('link', { name: 'Cluster' });
    expect(link.closest('.nav-group')?.textContent).toContain('Workspace');
    expect(await screen.findByText('worker-a')).toBeTruthy();
  });
  it('offers a first-level history destination and selects only authorized application histories', async () => {
    open('/delivery');
    await screen.findByRole('heading', { name: 'Deployment history', level: 1 });
    expect(within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('link', { name: 'Deployment history' }).getAttribute('href')).toBe('/delivery');
    const picker = await screen.findByRole('combobox', { name: 'Application history' });
    expect(within(picker).getAllByRole('option')).toHaveLength(2);
    expect(screen.queryByRole('option', { name: /Hidden history/ })).toBeNull();
    expect(screen.getByRole('link', { name: 'Open application' }).getAttribute('href')).toBe('/applications/shop/overview');
  });
  it('switches application scope without carrying another application’s exact plan comparison', async () => {
    open('/delivery?application=shop&deliveryView=plans&compareFrom=private&compareTo=private&planSearch=old&keep=context');
    await userEvent.setup().selectOptions(await screen.findByRole('combobox', { name: 'Application history' }), 'tools');
    const query = new URL(screen.getByLabelText('Address').textContent!, 'http://localhost').searchParams;
    expect(query.get('application')).toBe('tools');
    expect(query.get('keep')).toBe('context');
    expect(query.get('deliveryView')).toBe('plans');
    for (const key of ['compareFrom', 'compareTo', 'planSearch']) expect(query.has(key)).toBe(false);
  });
  it('does not silently replace a missing bookmarked application with another scope', async () => {
    const requests = open('/delivery?application=removed');
    expect(await screen.findByText('Selected application history is unavailable')).toBeTruthy();
    expect(requests.some(path => path.endsWith('/plans') || path.endsWith('/operations'))).toBe(false);
  });
  it('does not request histories without the global capability', async () => {
    const requests = open('/delivery', false);
    expect(await screen.findByText('Deployment history unavailable')).toBeTruthy();
    expect(requests.some(path => path.endsWith('/applications') || path.endsWith('/plans') || path.endsWith('/operations'))).toBe(false);
  });
  it('exposes the Plans tab and direct evidence links to history readers without source evaluation', async () => {
    open('/applications/shop/overview');
    await screen.findByRole('heading', { name: 'Shop' });
    const navigation = within(screen.getByRole('navigation', { name: 'Application views' }));
    expect(navigation.getByRole('link', { name: 'Plans' }).getAttribute('href')).toBe('/applications/shop/plans');
    const connections = within(screen.getByRole('region', { name: 'Explore application' }));
    expect(connections.getByRole('link', { name: /Previous plans/ }).getAttribute('href')).toBe('/applications/shop/plans');
    expect(connections.getByRole('link', { name: /Runs & logs/ }).getAttribute('href')).toBe('/applications/shop/activity');
    expect(connections.getByRole('link', { name: /Compare revisions/ }).getAttribute('href')).toBe('/applications/shop/activity?history=revisions');
  });
});
