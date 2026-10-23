import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { seconds, Trains, type TrainsDeployed, type TrainsStatus } from './Trains';

const status: TrainsStatus = {
  schema: 'piceli.trains.v1',
  trains: [{
    train: 'shop', state: 'running',
    current: {
      run: '20261023T101500Z-3f2a1c', started: '2026-10-23T10:15:00Z', elapsed: 125, phase: 'trials', branches: ['feat-a', 'fix-c'], verdicts: { 'feat-a': 'pending', 'fix-c': 'pending' },
      trial: { id: 1, branches: ['feat-a', 'fix-c'], base: 'a'.repeat(40), started: '2026-10-23T10:15:00Z' },
      step: { trial: 1, name: 'cargo-test', kind: 'cargo-test', host: 'local', started: '2026-10-23T10:16:02Z', elapsed: 41, tail: 'test api::retries ... ok\ntest core::value ... ok' },
      done: [{ trial: 1, name: 'merge', verdict: 'passed', duration: 0.1, host: 'local' }, { trial: 1, name: 'cargo-check', verdict: 'passed', duration: 52.4, host: 'local' }],
    },
    queue: [
      { id: 'q-20261023T101700000000Z-ab12', state: 'waiting', position: 1, branches: ['feat-d', 'feat-e'], promote: true, push: true, queued_at: '2026-10-23T10:17:00Z', started_at: null, run: null, resumed: 0 },
      { id: 'q-20261023T101800000000Z-cd34', state: 'waiting', position: 2, branches: ['fix-f'], promote: false, push: false, queued_at: '2026-10-23T10:18:00Z', started_at: null, run: null, resumed: 0 },
    ],
    runs: [{
      run: '20261022T090000Z-aaaaaa', state: 'ejected', ok: false, started: '2026-10-22T09:00:00Z', duration: 412.7, host: 'local', head: 'b'.repeat(40),
      branches: [{ branch: 'feat-x', verdict: 'joined' }, { branch: 'fix-y', verdict: 'ejected', step: 'fitness', reason: 'train-step-failed' }],
      trials: 3, steps: { merge: 0.2, 'cargo-test': 81.2, fitness: 3.1 }, tag: 'tested/2026-10-22.1', promote: 'promoted', push: 'pushed', flaky: 1,
    }],
    trends: [{ step: 'cargo-test', samples: 8, median: 80.1, last: 120.4, last_run: '20261022T090000Z-aaaaaa', ratio: 1.5, trend: 'slower' }],
    flaky: [{ key: '-p api --test http retries_once', test: 'retries_once', target: '-p api --test http', count: 3, last_seen: '2026-10-22T09:05:00Z', last_run: '20261022T090000Z-aaaaaa', last_branches: ['feat-x'], last_tail: 'thread panicked at timeout' }],
  }],
};
const deployed: TrainsDeployed = {
  configured: true, generated_at: '2026-10-23T10:16:30Z',
  rows: [
    { kind: 'environment', name: 'main', where: 'my-cluster/shop', state: 'deployed', revision: { commit: 'b'.repeat(40), tag: null }, pinned: null, train: { train: 'shop', run: '20261022T090000Z-aaaaaa', tag: 'tested/2026-10-22.1', head: 'b'.repeat(40), joined: ['feat-x'], pushed: true }, matched: 'commit' },
    { kind: 'nix-host', name: 'web-1', where: null, state: 'deployed', revision: { commit: 'c'.repeat(40), tag: 'web-v1' }, pinned: null, train: null, matched: null },
  ],
};

function open(body: TrainsStatus, targets: TrainsDeployed = { configured: false, rows: [] }) {
  vi.stubGlobal('fetch', vi.fn(async (request: Request | string) => {
    const url = typeof request === 'string' ? request : request.url;
    return Response.json(url.endsWith('/trains/deployed') ? targets : body);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter><Trains /></MemoryRouter></QueryClientProvider>);
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('shows the running step with its host, elapsed time and log tail', async () => {
  open(status);
  const running = await screen.findByRole('region', { name: 'Running shop' });
  expect(within(running).getByText('cargo-test')).toBeTruthy();
  expect(within(running).getByText(/41 s on local/)).toBeTruthy();
  expect(within(running).getByLabelText('Log of cargo-test').textContent).toContain('core::value ... ok');
  expect(within(running).getByText('cargo-check')).toBeTruthy();
  expect(within(running).queryByText('merge')).toBeNull();
});

it('shows the queue with the flags and queue time of each request', async () => {
  open(status);
  const queue = await screen.findByRole('region', { name: 'Queue of shop' });
  expect(within(queue).getByText('#1 feat-d feat-e')).toBeTruthy();
  expect(within(queue).getByText(/--promote --push/)).toBeTruthy();
  expect(within(queue).getByText('#2 fix-f')).toBeTruthy();
});

it('shows why a run failed midway', async () => {
  open({ ...status, trains: [{ ...status.trains[0], runs: [{ ...status.trains[0].runs[0], state: 'failed', reason: 'train-git-failed' }] }] });
  const runs = await screen.findByRole('region', { name: 'Recent runs' });
  expect(within(runs).getByText(/train-git-failed/)).toBeTruthy();
});

it('lists recent runs, trends and flaky tests', async () => {
  open(status);
  const runs = await screen.findByRole('region', { name: 'Recent runs' });
  expect(within(runs).getByText(/fix-y ejected \(fitness\)/)).toBeTruthy();
  expect(within(runs).getByText(/tested\/2026-10-22.1 · pushed/)).toBeTruthy();
  const trends = screen.getByRole('region', { name: 'Step trends' });
  expect(within(trends).getByText(/median 80 s · last 2.0 min/)).toBeTruthy();
  expect(within(trends).getByText('Slower')).toBeTruthy();
  const flaky = screen.getByRole('region', { name: 'Flaky tests' });
  expect(within(flaky).getByText('×3')).toBeTruthy();
});

it('joins targets with the run that produced them', async () => {
  open(status, deployed);
  const region = await screen.findByRole('region', { name: 'Deployed by train' });
  expect(within(region).getByText(/Train shop, run 20261022T090000Z-aaaaaa \(tested\/2026-10-22.1\): feat-x · matched by commit/)).toBeTruthy();
  expect(within(region).getByText('No train run produced this')).toBeTruthy();
});

it('says when no train has run', async () => {
  open({ schema: 'piceli.trains.v1', trains: [] });
  expect(await screen.findByText('No train has run yet')).toBeTruthy();
});

it('formats durations', () => {
  expect(seconds(4.24)).toBe('4.2 s');
  expect(seconds(41)).toBe('41 s');
  expect(seconds(412.7)).toBe('6.9 min');
  expect(seconds(7200)).toBe('2.0 h');
  expect(seconds(null)).toBe('—');
});
