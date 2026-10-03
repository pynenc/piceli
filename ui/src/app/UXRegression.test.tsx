import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { App } from './App';
import type { Application, Capabilities, Resource } from '../api/generated';

const target = { id: 'target-shop', name: 'test-cluster', namespace: 'shop' };
const otherTarget = { id: 'target-tools', name: 'test-cluster', namespace: 'tools' };
const application: Application = { id: 'shop', name: 'Shop', target, definition_kind: 'inventory', ownership: 'inventory', freshness: { state: 'connected' } };
const otherApplication: Application = { ...application, id: 'tools', name: 'Tools', target: otherTarget };
const capabilities: Capabilities = { principal: { id: 'local', name: 'Local operator' }, targets: [target, otherTarget], actions: { inspect: { allowed: true } } };
const resource: Resource = {
  id: 'deployment-api', identity: { target_id: target.id, api_version: 'apps/v1', kind: 'Deployment', namespace: 'shop', name: 'api', uid: 'deployment-uid' },
  presence: 'present', ownership: 'managed', health: 'unknown', relation: 'unknown', ports: [8080],
  capabilities: { conditions: { allowed: true }, manifest: { allowed: true }, logs: { allowed: true }, access: { allowed: true } },
  manifest: { metadata: { name: 'api' } }, conditions: [{ type: 'Available', status: 'True' }],
};
let selectedResource: Resource;
let requests: { path: string; method: string; query: URLSearchParams }[];

beforeEach(() => {
  selectedResource = resource;
  requests = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const url = new URL(request.url);
    requests.push({ path: url.pathname, method: request.method, query: url.searchParams });
    const path = url.pathname;
    if (path.endsWith('/capabilities')) return Response.json(capabilities);
    if (path.endsWith('/profiles')) return Response.json({ active: null, profiles: [] });
    if (path.endsWith('/applications')) return Response.json({ items: [application, otherApplication], cursor: 'apps' });
    if (path.endsWith('/log-sources')) return Response.json({ items: [{ pod: { ...resource.identity, api_version: 'v1', kind: 'Pod', name: 'api-pod', uid: 'pod-uid' }, containers: ['api', 'sidecar'], phase: 'Running' }], freshness: application.freshness });
    if (path.endsWith('/logs')) return Response.json({ lines: [], cursor: 'logs' });
    if (path.endsWith('/access-sessions')) return Response.json({ items: [] });
    if (path.endsWith('/resources/deployment-api')) return Response.json(selectedResource);
    if (path.endsWith('/resources')) return Response.json({ items: [selectedResource], cursor: 'resources', freshness: application.freshness, partial: [] });
    if (path.endsWith('/applications/shop')) return Response.json(application);
    return Response.json({ code: 'not-found', message: 'Not part of this test fixture.', correlation_id: 'test' }, { status: 404 });
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function Location() {
  const location = useLocation();
  return <output aria-label="Current address">{location.pathname}{location.search}</output>;
}
function open(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App /><Location /></MemoryRouter></QueryClientProvider>);
}
function address() { return new URL(screen.getByLabelText('Current address').textContent!, 'http://localhost'); }

describe('navigation and session capabilities', () => {
  it('opens an inventory session at applications and exposes only available navigation', async () => {
    open('/');
    await screen.findByRole('heading', { name: 'Applications' });
    expect(address().pathname).toBe('/applications');
    const nav = screen.getByRole('navigation', { name: 'Main navigation' });
    expect(within(nav).getByRole('link', { name: /Applications/ }).getAttribute('href')).toBe('/applications');
    for (const unavailable of ['Pipeline', 'Cluster build', 'GitOps', 'Sources', 'Environments']) {
      expect(within(nav).queryByRole('link', { name: new RegExp(unavailable) })).toBeNull();
    }
  });

  it.each([
    ['/pipeline', 'Pipeline unavailable', '/api/v1/pipeline'],
    ['/cluster-build', 'Cluster build unavailable', '/api/v1/cluster-build'],
    ['/cluster', 'Cluster unavailable', '/api/v1/cluster'],
    ['/environments', 'Environments unavailable', '/api/v1/environments'],
    ['/gitops', 'GitOps unavailable', '/api/v1/gitops'],
    ['/composition', 'Composition views unavailable', '/api/v1/composition'],
    ['/composition/sources', 'Composition views unavailable', '/api/v1/composition'],
  ])('does not request privileged data when opening %s without its capability', async (path, title, apiPath) => {
    open(path);
    await screen.findByText(title);
    await screen.findByText('Local operator');
    expect(requests.some(request => request.path.startsWith(apiPath))).toBe(false);
    expect(requests.every(request => request.method === 'GET')).toBe(true);
  });

  it('combines deep-linked application search and target filters without losing either', async () => {
    open('/applications?q=Shop&target=target-shop');
    const user = userEvent.setup();
    await screen.findByRole('link', { name: /Shop/ });
    expect(screen.queryByRole('link', { name: /Tools/ })).toBeNull();
    expect((screen.getByRole('searchbox', { name: 'Search applications' }) as HTMLInputElement).value).toBe('Shop');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Target' }), otherTarget.id);
    await screen.findByRole('heading', { name: 'No matching applications' });
    expect(address().searchParams.get('q')).toBe('Shop');
    expect(address().searchParams.get('target')).toBe(otherTarget.id);
    await user.clear(screen.getByRole('searchbox', { name: 'Search applications' }));
    await screen.findByRole('link', { name: /Tools/ });
    expect(address().searchParams.has('q')).toBe(false);
    expect(address().searchParams.get('target')).toBe(otherTarget.id);
  });

  it('keeps keyboard navigation focused on the new main content', async () => {
    open('/applications');
    await userEvent.setup().click(await screen.findByRole('link', { name: /Shop/ }));
    await screen.findByRole('heading', { name: 'Shop' });
    expect(address().pathname).toBe('/applications/shop/overview');
    expect(document.activeElement).toBe(screen.getByRole('main'));
    expect(screen.getByRole('link', { name: 'Skip to content' }).getAttribute('href')).toBe('#main');
  });
});

describe('resource inspector continuity', () => {
  it('preserves filters and relationship view through panel selection and returns focus on close', async () => {
    open('/applications/shop/resources?filter=Deployment&view=relationships');
    const user = userEvent.setup();
    const inspect = await screen.findByRole('button', { name: 'Inspect Deployment api in shop' });
    await user.click(inspect);
    const dialog = await screen.findByRole('dialog', { name: 'Resource details' });
    await user.click(await within(dialog).findByRole('button', { name: 'Manifest' }));
    expect(await screen.findByLabelText('Resource manifest')).toBeTruthy();
    expect(address().searchParams.get('resource')).toBe(resource.id);
    expect(address().searchParams.get('panel')).toBe('manifest');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(address().searchParams.get('filter')).toBe('Deployment');
    expect(address().searchParams.get('view')).toBe('relationships');
    expect(address().searchParams.has('resource')).toBe(false);
    expect(address().searchParams.has('panel')).toBe(false);
    await waitFor(() => expect(document.activeElement).toBe(inspect));
  });

  it('reads logs for the exact selected workload, pod and container, then opens its access panel', async () => {
    open('/applications/shop/resources?resource=deployment-api&panel=logs');
    const user = userEvent.setup();
    const container = await screen.findByRole('combobox', { name: 'Container' });
    await user.selectOptions(container, 'sidecar');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Instance' }), 'previous');
    await waitFor(() => expect(requests.some(request => request.path.endsWith('/logs') && request.query.get('container') === 'sidecar' && request.query.get('previous') === 'true')).toBe(true));
    const logRead = requests.filter(request => request.path.endsWith('/logs')).at(-1)!;
    expect(logRead.query.get('resource_uid')).toBe('deployment-uid');
    expect(logRead.query.get('pod_uid')).toBe('pod-uid');
    const panels = screen.getByRole('navigation', { name: 'Resource panels' });
    await user.click(within(panels).getByRole('button', { name: 'Access' }));
    await screen.findByRole('button', { name: 'Start local connection' });
    expect(address().searchParams.get('resource')).toBe(resource.id);
    expect(address().searchParams.get('panel')).toBe('access');
    expect(requests.some(request => request.path.endsWith('/access-sessions'))).toBe(true);
    expect(requests.every(request => request.method === 'GET')).toBe(true);
  });

  it('falls back to summary for a forbidden deep-linked panel without reading logs or starting access', async () => {
    selectedResource = { ...resource, capabilities: { manifest: { allowed: true }, logs: { allowed: false }, access: { allowed: false } } };
    open('/applications/shop/resources?resource=deployment-api&panel=logs');
    const panels = await screen.findByRole('navigation', { name: 'Resource panels' });
    expect(within(panels).getByRole('button', { name: 'Summary' }).getAttribute('aria-current')).toBe('page');
    expect(within(panels).queryByRole('button', { name: 'Logs' })).toBeNull();
    expect(within(panels).queryByRole('button', { name: 'Access' })).toBeNull();
    expect(requests.some(request => /\/(?:logs|log-sources|access-sessions)$/.test(request.path))).toBe(false);
  });
});
