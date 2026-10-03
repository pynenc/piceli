import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';
import type { Capabilities, WorkspaceLogBatch, WorkspaceLogSources } from '../../api/generated';
import { LogWorkspace } from './LogWorkspace';

const capabilities: Capabilities = { principal: { id: 'local', name: 'Local' }, targets: [], actions: { logs: { allowed: true }, profile_scopes: { allowed: true } } };
const pod = (scope: string, name: string, kind: string, workload: string) => ({ scope_id: scope, pod: { target_id: 't', api_version: 'v1', kind: 'Pod', namespace: 'shop', name, uid: `uid-${name}` }, workload: { kind, name: workload }, containers: ['api'], phase: 'Running', restarts: 0 });
const sources: WorkspaceLogSources = {
  scopes: [{ id: 'shop', name: 'Shop', kind: 'application', cluster: 'east', namespace: 'shop', profile: 'east' }, { id: 'env-main', name: 'main', kind: 'environment', cluster: 'east', namespace: 'shop-main' }],
  items: [pod('shop', 'api-1', 'Deployment', 'api'), pod('env-main', 'web-1', 'StatefulSet', 'web')],
  freshness: { state: 'connected' }, partial: [],
};
const batch: WorkspaceLogBatch = {
  cursor: 'c',
  streams: [{ scope_id: 'shop', pod_name: 'api-1', pod_uid: 'uid-api-1', container: 'api', state: 'ok', lines: 2 }, { scope_id: 'env-main', pod_name: 'web-1', pod_uid: 'uid-web-1', container: 'api', state: 'unavailable', code: 'ui-logs-unavailable' }],
  lines: [{ at: '2026-10-03T10:00:01.000000000Z', stream: 0, level: 'info', text: 'listening' }, { at: '2026-10-03T10:00:02.000000000Z', stream: 0, level: 'error', text: 'token=[REDACTED] failed' }],
};
let requests: URL[];
beforeEach(() => {
  requests = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const url = new URL(request.url);
    requests.push(url);
    if (url.pathname.endsWith('/logs/sources')) return Response.json(sources);
    if (url.pathname.endsWith('/logs/lines')) return Response.json(batch);
    if (url.pathname.endsWith('/profiles')) return Response.json({ active: 'east', profiles: [{ name: 'east', available: true }, { name: 'west', available: true }], scopes: [] });
    return Response.json({});
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

let location = '';
function Where() { location = `${useLocation().pathname}${useLocation().search}`; return null; }
function open(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><LogWorkspace capabilities={capabilities} /><Where /></MemoryRouter></QueryClientProvider>);
}

it('aggregates scopes, merges lines and keeps unreadable containers explicit', async () => {
  open('/logs');
  const lines = await screen.findByRole('region', { name: 'Container log lines' });
  expect(within(lines).getByText('listening')).toBeTruthy();
  expect(within(lines).getByText('token=[REDACTED] failed')).toBeTruthy();
  expect(screen.getByText('1 container not read')).toBeTruthy();
  expect(screen.getByLabelText('Error lines shown').textContent).toBe('1');
  const read = requests.find(url => url.pathname.endsWith('/logs/lines'))!;
  expect(read.searchParams.getAll('stream')).toEqual(['shop/api-1/uid-api-1/api', 'env-main/web-1/uid-web-1/api']);
  expect(screen.getByRole('group', { name: 'east' })).toBeTruthy();
});

it('deep links filter by scope and workload and keep filters in the address', async () => {
  open('/logs?scope=shop&workload=Deployment/api');
  await screen.findByRole('region', { name: 'Container log lines' });
  const sourceRead = requests.find(url => url.pathname.endsWith('/logs/sources'))!;
  expect(sourceRead.searchParams.getAll('scope')).toEqual(['shop']);
  await waitFor(() => expect(requests.filter(url => url.pathname.endsWith('/logs/lines')).at(-1)!.searchParams.getAll('stream')).toEqual(['shop/api-1/uid-api-1/api']));
  const user = userEvent.setup();
  await user.click(screen.getByRole('checkbox', { name: /Error/ }));
  expect(location).toContain('level=error');
  await user.click(screen.getByRole('button', { name: 'Pause live tail' }));
  expect(location).toContain('follow=0');
  await user.selectOptions(screen.getByRole('combobox', { name: 'Time range' }), '15m');
  await waitFor(() => expect(requests.filter(url => url.pathname.endsWith('/logs/lines')).at(-1)!.searchParams.get('since_seconds')).toBe('900'));
  await user.click(within(screen.getByRole('region', { name: 'Container log lines' })).getByText('listening'));
  const detail = screen.getByRole('region', { name: 'Selected log line' });
  expect(within(detail).getByText('Shop · shop')).toBeTruthy();
  expect(within(detail).getByRole('link', { name: 'Open pod in Resources' }).getAttribute('href')).toBe('/applications/shop/resources?filter=api-1');
});

it('adds another profile scope through the address, read-only', async () => {
  open('/logs');
  await screen.findByRole('region', { name: 'Container log lines' });
  const user = userEvent.setup();
  await user.click(screen.getByText('Add another profile'));
  await user.selectOptions(screen.getByRole('combobox', { name: 'Profile' }), 'west');
  await user.click(screen.getByRole('button', { name: 'Add scope' }));
  expect(location).toContain('profile=west%3Adefault');
  await waitFor(() => expect(requests.some(url => url.pathname.endsWith('/profiles/scopes'))).toBe(true));
});
