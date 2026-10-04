import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { Machines, type MachinesStatus } from './Machines';

const base: MachinesStatus = {
  schema: 'piceli.infra.status.v1', name: 'edge', state: 'applied', ref: 'machines.py:infra', updated_at: '2026-10-04T10:00:00Z', tofu: '1.12.6',
  backend: { kind: 'local' }, resources: 4,
  servers: [{
    name: 'edge-1', provider: 'hetzner', type: 'cax11', location: 'fsn1', image: 'debian-12', state: 'created', id: '42', status: 'running',
    ipv4: '192.0.2.10', ipv6: '2001:db8::1', fixed_ipv4: true, firewall: 'edge', host_keys: [{ type: 'ssh-ed25519', fingerprint: 'SHA256:abc' }],
    install: { declared: true, state: 'installed' },
    cluster: { name: 'edge-1', api: 'https://edge-1.example.net:6443', profile: 'edge-1', registered: true, registered_at: '2026-10-04T10:05:00Z', nodes: { nodes: 1, ready: 1 } },
    monthly_net: 3.79,
  }],
  records: [{ key: 'www.example.com/A', type: 'A', state: 'created', servers: ['edge-1'], value: null, ttl: 300 }],
  estimate: { currency: 'EUR', monthly_net: 4.29, complete: true },
  commands: { plan: 'piceli infra plan machines.py:infra', status: 'piceli infra status machines.py:infra' },
};

function open(status: MachinesStatus) {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json(status)));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter><Machines /></MemoryRouter></QueryClientProvider>);
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('lists each server with its addresses, install, cluster and cost', async () => {
  open(base);
  expect(await screen.findByText('192.0.2.10 (fixed) · 2001:db8::1')).toBeTruthy();
  expect(screen.getByText('4.29 EUR / month')).toBeTruthy();
  expect(screen.getByText('3.79 EUR / month')).toBeTruthy();
  expect(screen.getByText(/1\/1 nodes ready/)).toBeTruthy();
  expect(screen.getByText('piceli infra plan machines.py:infra')).toBeTruthy();
  expect(screen.getByText('www.example.com/A')).toBeTruthy();
});

it('says when no price is known', async () => {
  open({ ...base, estimate: null, servers: [{ ...base.servers[0], monthly_net: null, cluster: null, ipv4: null, fixed_ipv4: false }] });
  expect(await screen.findByText('Not available')).toBeTruthy();
  expect(screen.getByText('None declared')).toBeTruthy();
  expect(screen.getByText(/No IPv4/)).toBeTruthy();
});
