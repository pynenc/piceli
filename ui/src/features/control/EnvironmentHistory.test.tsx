import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { CompositionHistory, EnvironmentHistory, outcome, type History, type HistoryRun } from './EnvironmentHistory';

const sha = (c: string) => `sha256:${c.repeat(64)}`;
const base: HistoryRun = {
  id: 'r2', run_id: '20261001T092110500000Z-aaaa1111', started_at: '2026-10-01T09:20:00Z', finished_at: '2026-10-01T09:22:00Z',
  state: 'deployed', action: 'deployed', trigger: 'push product/main',
  sources: [{ name: 'product', commit: '3f9c2d1e8a7b4c5d6e0f1a2b3c4d5e6f7a8b9c0d', ref: 'refs/heads/main' }],
  plan_hash: sha('d'), combined_hash: sha('e'), approved_by: { via: 'cli', at: '2026-10-01T09:19:30Z' }, release: 'shop-2b3c',
  components: [{ name: 'web', source: 'product', commit: '3f9c2d1', digest: sha('a'), state: 'synced', built: true }, { name: 'worker', digest: sha('b'), state: 'unchanged', built: false }],
  built: ['web'], rolled: ['web'], unchanged: ['worker'],
  plan: { counts: { update: 1, 'no-op': 7 }, changes: [{ operation: 'update', kind: 'Deployment', name: 'web' }], changes_total: 1 },
  checks: { passed: true, verification: false, results: [{ name: 'web-home', type: 'http', passed: true, detail: 'GET / returned 200', duration: 0.084 }] },
  failure: null, stages: [{ name: 'apply', state: 'done', seconds: 4.5 }], seconds: 7.7, recorded_by: 'controller+run',
};
const degraded: HistoryRun = {
  ...base, id: 'r3', run_id: 'r3', state: 'degraded', action: 'verified', trigger: 'checks-changed', approved_by: null, rolled: [], built: [], unchanged: ['web', 'worker'],
  checks: { passed: false, verification: true, results: [{ name: 'deliberate-failure', type: 'exec', passed: false, code: 'check-failed', detail: 'sh exited 1, expected 0', duration: 0.31 }] },
  failure: { stage: 'checks', reason: 'pipeline-checks-failed', failed_checks: [{ name: 'deliberate-failure', code: 'check-failed' }] },
};
const failed: HistoryRun = { ...base, id: 'wp@x', run_id: null, state: 'failed', reason: 'component-build-failed', plan: null, checks: null, stages: [], components: [], built: [], rolled: [], unchanged: [], failure: { reason: 'component-build-failed', failed_checks: [], log_tail: 'npm ERR! missing script: build' } };
const history: History = { configured: true, available: true, generated_at: '2026-10-01T09:30:00Z', truncated: false, environments: [{ name: 'main', runs: [degraded, base] }, { name: 'wp-login', runs: [failed] }] };

function mount(node: React.ReactNode, body: unknown = history) {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json(body)));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>);
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('describes each outcome in words', () => {
  expect(outcome(base)).toBe('Deployed; rolled web');
  expect(outcome(degraded)).toBe('Checks failed; the release keeps running');
  expect(outcome({ ...base, action: 'verified', rolled: [] })).toBe('Verified (checks changed); rolled nothing');
  expect(outcome(failed)).toBe('Failed (component-build-failed)');
});

it('lists an environment’s runs newest first with trigger, approver, components, checks and failures', async () => {
  mount(<EnvironmentHistory env="main" />);
  const runs = await screen.findByRole('list', { name: 'Runs of main' });
  const items = within(runs).getAllByRole('listitem', { name: undefined }).filter(item => item.parentElement === runs);
  expect(items).toHaveLength(2);
  expect(items[0].textContent).toContain('Checks failed; the release keeps running');
  expect(items[0].textContent).toContain('deliberate-failure');
  expect(items[0].textContent).toContain('sh exited 1, expected 0');
  expect(items[0].textContent).toContain('piceli explain pipeline-checks-failed');
  expect(items[1].textContent).toContain('Deployed; rolled web');
  expect(items[1].textContent).toContain('piceli gitops approve (CLI)');
  expect(items[1].textContent).toContain('push product/main');
  expect(items[1].textContent).toContain('Deployment/web');
  expect(items[1].textContent).toContain('built');
  expect(items[1].textContent).toContain('unchanged');
  expect(items[1].textContent).toContain(sha('d'));
});

it('shows a failed build’s log tail and says when no history is published', async () => {
  mount(<CompositionHistory selected="wp-login" onSelect={() => undefined} />);
  expect(await screen.findByLabelText('Failure log tail')).toHaveProperty('textContent', 'npm ERR! missing script: build');
  cleanup();
  mount(<EnvironmentHistory env="main" />, { ...history, available: false, environments: [{ name: 'main', runs: [] }] });
  expect(await screen.findByText('No run history published')).toBeTruthy();
});
