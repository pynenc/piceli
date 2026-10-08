import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { DevBuilds, type DevBuildsStatus } from './DevBuilds';

const status: DevBuildsStatus = {
  schema: 'piceli.ui-dev-builds.v1', state: 'running', node: 'builder-1', slots: 5, scheduler_at: '2026-10-08T07:00:00Z',
  running: [{ run: 'r1', requester: 'agent-7', priority: 'agent', profile: 'rust', started_at: '2026-10-08T07:00:00Z', waited_seconds: 12 }],
  queued: [{ run: 'r2', requester: 'coordinator', priority: 'round', profile: 'rust', position: 1, waited_seconds: 4 }],
  recent: [{ run: 'r0', requester: 'agent-3', priority: 'agent', state: 'failed', reason: 'dev-command-failed', durations: { build: 41, test: 12 }, tests: { passed: 211, failed: 1 }, cache: { lineage: 'shop-rust/0', warm: true, crates_compiled: 3, hit_ratio: 0.99 }, finished_at: '2026-10-08T06:59:00Z' }],
  cache: { used_bytes: 30 * 1024 ** 3, max_bytes: 100 * 1024 ** 3 },
};

function open(value: DevBuildsStatus) {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json(value)));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter><DevBuilds /></MemoryRouter></QueryClientProvider>);
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('shows slots, the queue in order, recent results and cache use', async () => {
  open(status);
  expect(await screen.findByText('1 / 5')).toBeTruthy();
  expect(screen.getByText('#1 r2')).toBeTruthy();
  expect(screen.getByText(/build 41 s · test 12 s · 211 passed, 1 failed · warm · 3 compiled · 99 % fresh/)).toBeTruthy();
  expect(screen.getByText(/dev-command-failed/)).toBeTruthy();
  expect(screen.getByText('30.0 GiB (30 %)')).toBeTruthy();
});

it('explains a cluster without development builds', async () => {
  open({ schema: 'piceli.ui-dev-builds.v1', state: 'not-installed' });
  expect(await screen.findByText('Development builds are not installed')).toBeTruthy();
});

it('says when the queue stopped publishing', async () => {
  open({ ...status, state: 'stale' });
  expect(await screen.findByText('The queue is not publishing')).toBeTruthy();
});
