import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { AccessSession, Resource } from '../../api/generated';
import { AccessPanel } from './AccessPanel';

const resource: Resource = {
  id: 'service-api',
  identity: { target_id: 'target', api_version: 'v1', kind: 'Service', namespace: 'shop', name: 'api', uid: 'resource-uid' },
  presence: 'present',
  ownership: 'managed',
  ports: [8080],
  capabilities: { access: { allowed: true } },
};
const session: AccessSession = {
  id: 'a'.repeat(32), application_id: 'shop', resource: resource.identity,
  principal_id: 'operator', binding_location: 'local_client', state: 'pending',
  endpoint: null, local_port: null, remote_port: 8080, expires_at: '2030-01-01T00:00:00Z',
};
const secret = 'private-pairing-secret-for-a-disposable-test';

function mount(selected: Resource = resource) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><AccessPanel applicationId="shop" resource={selected} /></QueryClientProvider>);
  return client;
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('shows a one-time remote ticket without placing its secret in the command or claiming a local port is ready', async () => {
  let issued = false;
  let remoteState: AccessSession['state'] = 'pending';
  const requests: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    requests.push(`${request.method} ${path}`);
    if (path.endsWith('/capabilities')) return Response.json({ mode: 'cluster', principal: { id: 'operator', name: 'Operator' }, targets: [], actions: {} });
    if (path.endsWith('/remote-access')) {
      if (request.method === 'POST') { issued = true; return Response.json({ session, pairing_secret: secret }); }
      return Response.json({ items: issued ? [{ ...session, state: remoteState }] : [] });
    }
    if (path.endsWith(`/remote-access/${session.id}`) && request.method === 'DELETE') {
      issued = false;
      return Response.json({ ...session, state: 'stopped' });
    }
    return new Response(null, { status: 404 });
  }));
  const client = mount();
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Issue connection ticket' }));
  expect(await screen.findByText(secret)).toBeTruthy();
  const command = screen.getByText(/piceli ui connect --server/);
  expect(command.textContent).toContain(`--ticket ${session.id}`);
  expect(command.textContent).toContain('--kubeconfig /path/to/kubeconfig');
  expect(command.textContent).not.toContain(secret);
  expect(screen.getByText(/Pending means no local port is open yet/)).toBeTruthy();
  expect(requests.some(value => value.includes('/access-sessions'))).toBe(false);
  const ticket = await screen.findByText(/On your local client \(client-reported\)/);
  remoteState = 'connecting';
  await client.invalidateQueries({ queryKey: ['remote-access', 'shop'] });
  await waitFor(() => expect(screen.queryByText(secret)).toBeNull());
  expect(screen.getByText(/pairing secret can no longer be used/)).toBeTruthy();
  await user.click(within(ticket.closest('.access-session') as HTMLElement).getByRole('button', { name: 'Stop connection' }));
  expect(screen.queryByText(secret)).toBeNull();
});

it('uses an unprivileged local default for a remote Service on port 80', async () => {
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    if (path.endsWith('/capabilities')) return Response.json({ mode: 'cluster', principal: { id: 'operator', name: 'Operator' }, targets: [], actions: {} });
    if (path.endsWith('/remote-access')) return Response.json(request.method === 'POST' ? { session: { ...session, remote_port: 80 }, pairing_secret: secret } : { items: [] });
    return new Response(null, { status: 404 });
  }));
  mount({ ...resource, ports: [80] });
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Issue connection ticket' }));
  expect((await screen.findByText(/piceli ui connect --server/)).textContent).toContain('--local-port 8080');
});

it('keeps local-mode forwarding on the local service route', async () => {
  const requests: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    requests.push(`${request.method} ${path}`);
    if (path.endsWith('/capabilities')) return Response.json({ mode: 'local', principal: { id: 'local', name: 'Local operator' }, targets: [], actions: {} });
    if (path.endsWith('/access-sessions')) return Response.json(request.method === 'POST' ? { ...session, binding_location: 'server', state: 'ready', local_port: 8080, endpoint: 'http://127.0.0.1:8080' } : { items: [] });
    return new Response(null, { status: 404 });
  }));
  mount();
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Start local connection' }));
  expect(requests.some(value => value === 'POST /api/v1/applications/shop/access-sessions')).toBe(true);
  expect(requests.some(value => value.includes('/remote-access'))).toBe(false);
});
