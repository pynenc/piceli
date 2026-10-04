import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NamedEnvironmentActions } from './NamedEnvironmentActions';
import { EnvironmentChecks, checksSummary } from './CompositionInspector';
import type { Environment } from './Composition';

const allowed = { allowed: true, reason: null };
const refused = { allowed: false, reason: 'ui-operation-unavailable' };
const declared = { allowed: false, reason: 'gitops-env-stop-declared' };

function open(environment: Environment, options: Record<string, unknown>) {
  const posts: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    if (request.method === 'POST') { posts.push(new URL(request.url).pathname); return Response.json({ state: 'requested' }); }
    return Response.json({
      env: environment.name, promote: refused, approve: refused, wake: refused, refs: [], plan_hash: null, stopped_since: null,
      summary: { namespace: 'shop-rc', revision: {}, components: [] }, ...options,
    });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><NamedEnvironmentActions environment={environment} canChange /></QueryClientProvider>);
  return posts;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const running: Environment = { name: 'rc', state: 'deployed', health: 'healthy', namespace: 'shop-rc', revision: {}, components: [] };

it('stops a running named environment after a reviewed request', async () => {
  const posts = open(running, { stop: allowed, start: refused });
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Stop' }));
  expect(screen.queryByRole('button', { name: 'Start' })).toBeNull();
  await user.click(screen.getByRole('checkbox'));
  await user.click(screen.getByRole('button', { name: 'Confirm stop' }));
  await waitFor(() => expect(posts).toEqual(['/api/v1/composition/environments/rc/stop']));
});

it('starts an environment stopped on request, not a declared one', async () => {
  const posts = open({ ...running, state: 'stopped', reason: 'requested' }, { stop: refused, start: allowed });
  const user = userEvent.setup();
  expect(screen.queryByRole('button', { name: 'Wake' })).toBeNull();
  await user.click(await screen.findByRole('button', { name: 'Start' }));
  await user.click(screen.getByRole('checkbox'));
  await user.click(screen.getByRole('button', { name: 'Confirm start' }));
  await waitFor(() => expect(posts).toEqual(['/api/v1/composition/environments/rc/start']));
  cleanup();
  open({ ...running, state: 'stopped', reason: 'declared' }, { stop: declared, start: declared });
  const start = await screen.findByRole('button', { name: 'Start' });
  await waitFor(() => expect((start as HTMLButtonElement).disabled).toBe(true));
  expect(start.getAttribute('title')).toContain('Environment(stopped=True)');
});

it('shows the last checks of a rollout with each check', () => {
  const checks = {
    state: 'passed', passed: 2, total: 2, at: '2026-10-03T10:00:00Z', trigger: 'push web/main', run_id: 'r1', action: 'deployed',
    results: [{ name: 'web-home', passed: true, code: null }, { name: 'store-ready', passed: true, code: null }],
  };
  render(<EnvironmentChecks checks={checks} />);
  const card = screen.getByRole('region', { name: 'Last checks' });
  expect(card.textContent).toContain('2/2 passed');
  expect(card.textContent).toContain('After the rollout');
  expect(card.textContent).toContain('web-home');
  expect(card.textContent).toContain('store-ready');
  expect(checksSummary(checks)).toMatch(/^Checks passed 2\/2 · /);
});
