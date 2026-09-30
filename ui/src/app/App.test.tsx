import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { App } from './App';
import type { Application, Capabilities, Resource, ResourcePage } from '../api/generated';
const target = { id: 'target-1', name: 'test-cluster', namespace: 'shop' };
const application: Application = { id: 'scope', name: 'shop', target, definition_kind: 'inventory', ownership: 'inventory', health: 'unknown', relation: 'unknown', operation: 'idle', freshness: { state: 'connected', observed_at: '2026-09-29T12:00:00Z' } };
const capabilities: Capabilities = { principal: { id: 'local', name: 'Local operator' }, targets: [target], actions: { inspect: { allowed: true } } };
const resource: Resource = { id: 'deployment-api', identity: { target_id: target.id, api_version: 'apps/v1', kind: 'Deployment', namespace: 'shop', name: 'api', uid: 'uid-1' }, presence: 'present', ownership: 'managed', health: 'unknown', relation: 'unknown', capabilities: { manifest: { allowed: true } }, manifest: { data: '<script>danger()</script>' } };
let resourcePage: ResourcePage;
let forbidden: boolean;
beforeEach(() => {
  resourcePage = { items: [resource], cursor: 'c1', freshness: application.freshness, partial: [] };
  forbidden = false;
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const url = new URL(request.url).pathname;
    if (url.endsWith('/capabilities')) return Response.json(capabilities);
    if (url.endsWith('/applications')) return Response.json({ items: [application], cursor: 'c1' });
    if (url.endsWith('/resources/deployment-api')) return Response.json(resource);
    if (url.endsWith('/resources')) return forbidden ? Response.json({ code: 'forbidden', message: 'Resource read is not permitted.', correlation_id: 'test-1' }, { status: 403 }) : Response.json(resourcePage);
    return Response.json(application);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function open(path: string) { const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } }); render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>); return client; }
describe('observed application state', () => {
  it('shows where the local service runs and which actions are available', async () => {
    open('/applications');
    const environment = await screen.findByRole('region', { name: 'Execution environment' });
    expect(environment.textContent).toContain('Local process');
    expect(environment.textContent).toContain('1 Kubernetes scope');
    expect(environment.textContent).toContain('Observation');
    expect(environment.textContent).toContain('Actions and port forwards run on the host serving this UI.');
    expect(environment.textContent).toContain('This UI does not start CI jobs.');
  });
  it('does not infer hosting location from an authenticated scoped session', async () => {
    vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
      const path = new URL(request.url).pathname;
      if (path.endsWith('/capabilities')) return Response.json({ ...capabilities, mode: 'cluster', actions: { plan: { allowed: true }, deploy: { allowed: true } } });
      return Response.json({ items: [application], cursor: 'c1' });
    }));
    open('/applications');
    const environment = await screen.findByRole('region', { name: 'Execution environment' });
    expect(environment.textContent).toContain('Authenticated service');
    expect(environment.textContent).not.toContain('In-cluster service');
    expect(environment.textContent).toContain('Enabled actions run on this service host.');
    expect(environment.textContent).toContain('Review and deploy');
    expect(environment.textContent).not.toContain('This UI does not start CI jobs.');
  });
  it('does not offer observation after all scope grants are revoked', async () => {
    vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
      const path = new URL(request.url).pathname;
      if (path.endsWith('/capabilities')) return Response.json({ ...capabilities, mode: 'cluster', targets: [], actions: { inspect: { allowed: false, reason: 'not-authorized' } } });
      return Response.json({ items: [], cursor: 'c1' });
    }));
    open('/applications');
    const environment = await screen.findByRole('region', { name: 'Execution environment' });
    expect(environment.textContent).toContain('No accessible scopes');
    expect(environment.textContent).not.toContain('Observation');
  });
  it('does not advertise unavailable operation views in an inventory session', async () => {
    open('/applications/scope/activity');
    expect(await screen.findByText('Activity unavailable')).toBeTruthy();
    const tabs = screen.getByRole('navigation', { name: 'Application views' });
    expect(tabs.textContent).toContain('Resources');
    expect(tabs.textContent).not.toContain('Changes');
    expect(tabs.textContent).not.toContain('Activity');
  });
  it('keeps every typed character while synchronizing application and resource filters', async () => {
    open('/applications');
    const user = userEvent.setup();
    const search = await screen.findByRole('searchbox', { name: 'Search applications' });
    await user.type(search, 'shop');
    expect((search as HTMLInputElement).value).toBe('shop');
    await user.click(await screen.findByRole('link', { name: /shop/ }));
    const filter = await screen.findByRole('searchbox', { name: 'Filter resources' });
    await user.type(filter, 'Deployment');
    expect((filter as HTMLInputElement).value).toBe('Deployment');
    expect(await screen.findByRole('button', { name: /Inspect Deployment api/ })).toBeTruthy();
  });
  it('refreshes authoritative application state after the first resource observation', async () => {
    let observed = false;
    let applicationReads = 0;
    vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
      const path = new URL(request.url).pathname;
      if (path.endsWith('/capabilities')) return Response.json(capabilities);
      if (path.endsWith('/resources')) { observed = true; return Response.json(resourcePage); }
      applicationReads++;
      return Response.json({ ...application, freshness: observed ? application.freshness : { state: 'unavailable', observed_at: null } });
    }));
    open('/applications/scope/resources');
    expect(await screen.findByText('Unavailable observation')).toBeTruthy();
    expect(await screen.findByRole('button', { name: /Inspect Deployment api/ })).toBeTruthy();
    expect(await screen.findByText('Connected')).toBeTruthy();
    expect(applicationReads).toBe(2);
    expect(screen.queryByText('Unavailable observation')).toBeNull();
  });
  it('never treats a present resource as healthy or in sync', async () => {
    open('/applications/scope/resources');
    const row = await screen.findByRole('button', { name: /Deployment api/ });
    expect(row.textContent).toContain('Present');
    expect(row.textContent).toContain('Unknown');
    expect(screen.queryByText('Healthy')).toBeNull();
    expect(screen.queryByText('In sync')).toBeNull();
  });
  it('shows an empty partial observation distinctly from successful emptiness', async () => {
    resourcePage = { ...resourcePage, items: [], partial: [{ scope: 'apps/v1/Deployment', code: 'forbidden' }] };
    open('/applications/scope/resources');
    expect(await screen.findByText('Partial observation')).toBeTruthy();
    expect(screen.getByText('No resources visible in this partial observation')).toBeTruthy();
    expect(screen.queryByText('No resources observed')).toBeNull();
  });
  it('preserves a deep-linked inspector and treats manifest strings as text', async () => {
    open('/applications/scope/resources?resource=deployment-api&panel=manifest');
    const manifest = await screen.findByLabelText('Resource manifest');
    expect(manifest.textContent).toContain('<script>danger()</script>');
    expect(manifest.querySelector('script')).toBeNull();
    await userEvent.setup().keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).toBeNull();
  });
  it('retains last observed rows and selected resource after an unavailable empty refresh', async () => {
    const client = open('/applications/scope/resources');
    await userEvent.setup().click(await screen.findByRole('button', { name: /Inspect Deployment api/ }));
    expect(await screen.findByRole('dialog')).toBeTruthy();
    resourcePage = { items: [], cursor: 'failed-cursor', freshness: { state: 'unavailable' }, partial: [{ scope: 'apps/v1/Deployment', code: 'read-failed' }] };
    await act(async () => { await client.invalidateQueries({ queryKey: ['resources', 'scope'] }); });
    expect(await screen.findByText('Partial observation')).toBeTruthy();
    expect(screen.getByRole('dialog')).toBeTruthy();
    await userEvent.setup().keyboard('{Escape}');
    expect(screen.getByRole('button', { name: /Inspect Deployment api/ })).toBeTruthy();
    expect(screen.getByText('Unavailable observation')).toBeTruthy();
    expect(screen.queryByText('No resources visible in this partial observation')).toBeNull();
    resourcePage = { items: [], cursor: 'deleted-cursor', freshness: application.freshness, partial: [] };
    await act(async () => { await client.invalidateQueries({ queryKey: ['resources', 'scope'] }); });
    expect(await screen.findByText('No resources observed')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Inspect Deployment api/ })).toBeNull();
  });
  it('shows a denied read instead of claiming the scope is empty', async () => {
    forbidden = true;
    open('/applications/scope/resources');
    expect(await screen.findByText('Permission required')).toBeTruthy();
    expect(screen.queryByText('No resources observed')).toBeNull();
  });
});
