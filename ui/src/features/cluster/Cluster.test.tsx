import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { Cluster, type ClusterStatus } from './Cluster';

const base: ClusterStatus = { schema: 'piceli.ui-cluster.v1', state: 'ready', cluster: 'my-cluster', nodes: [], registry: { state: 'ready', host: 'registry.example:5000', ready: true, pods: [], storage: { claim: 'registry-data', phase: 'Bound', capacity: '20Gi', used_bytes: 2 * 1024 ** 3, used_source: 'du' } }, controller: null, ui: null };

function open(status: ClusterStatus) {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json(status)));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter><Cluster /></MemoryRouter></QueryClientProvider>);
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('says where the registry claim’s use was measured', async () => {
  open(base);
  expect(await screen.findByText('2.00 GiB · measured in the registry pod')).toBeTruthy();
});

it('reports unknown registry use as not reported instead of a node filesystem', async () => {
  open({ ...base, registry: { ...base.registry!, storage: { ...base.registry!.storage!, used_bytes: null, used_source: null } } });
  expect(await screen.findByText('registry-data · Bound · 20Gi')).toBeTruthy();
  expect(screen.getByText('Not reported')).toBeTruthy();
});
