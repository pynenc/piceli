import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NamedEnvironmentActions } from './NamedEnvironmentActions';
import type { Environment } from './Composition';

const allowed = { allowed: true, reason: null };
const original = {
  env: 'preview', approve: allowed, promote: allowed, wake: allowed,
  plan_hash: `sha256:${'a'.repeat(64)}` as string | null, stopped_since: '2026-10-01T10:00:00Z',
  refs: [{ source: 'product', branch: 'main', commit: 'b'.repeat(40) }],
  summary: { namespace: 'shop-preview', revision: { product: 'b'.repeat(40) }, components: ['api'] },
};
const initialEnvironment: Environment = { name: 'preview', state: 'approval-required', health: 'unknown', namespace: 'shop-preview', revision: original.summary.revision, components: [] };

function open() {
  let options = structuredClone(original);
  let environment = { ...initialEnvironment };
  let canChange = true;
  const posts: { path: string; body: unknown }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    if (request.method === 'POST') { const text = await request.text(); posts.push({ path: new URL(request.url).pathname, body: text ? JSON.parse(text) : null }); return Response.json({ state: 'requested' }); }
    return Response.json(options);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  const tree = () => <QueryClientProvider client={client}><NamedEnvironmentActions environment={environment} canChange={canChange} /></QueryClientProvider>;
  const view = render(tree());
  return {
    posts,
    refresh: async (change: Partial<typeof original>) => { options = { ...options, ...change }; await act(async () => { await client.invalidateQueries({ queryKey: ['composition', 'actions', environment.name] }); }); },
    route: (name: string) => { options = { ...options, env: name }; environment = { ...environment, name }; view.rerender(tree()); },
    capability: (value: boolean) => { canChange = value; view.rerender(tree()); },
    state: (value: string) => { environment = { ...environment, state: value }; view.rerender(tree()); },
  };
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each(['hash refresh', 'environment route'] as const)('requires a new acknowledgement after %s before approving', async change => {
  const fixture = open();
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Approve' }));
  await user.click(screen.getByRole('checkbox'));
  expect((screen.getByRole('button', { name: 'Confirm approve' }) as HTMLButtonElement).disabled).toBe(false);
  const nextHash = `sha256:${'c'.repeat(64)}`;
  if (change === 'hash refresh') await fixture.refresh({ plan_hash: nextHash });
  else fixture.route('stage');
  const checkbox = await screen.findByRole('checkbox');
  await waitFor(() => expect((checkbox as HTMLInputElement).checked).toBe(false));
  const confirm = screen.getByRole('button', { name: 'Confirm approve' });
  expect((confirm as HTMLButtonElement).disabled).toBe(true);
  await user.click(confirm);
  expect(fixture.posts).toEqual([]);
  await user.click(checkbox);
  await user.click(confirm);
  await waitFor(() => expect(fixture.posts).toHaveLength(1));
  expect(fixture.posts[0]).toEqual({ path: `/api/v1/composition/environments/${change === 'hash refresh' ? 'preview' : 'stage'}/approvals`, body: { plan_hash: change === 'hash refresh' ? nextHash : original.plan_hash } });
});

it.each(['session capability', 'action permission', 'missing hash', 'wrong environment'] as const)('refuses an acknowledged approval after losing %s', async change => {
  const fixture = open();
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Approve' }));
  await user.click(screen.getByRole('checkbox'));
  if (change === 'session capability') fixture.capability(false);
  else if (change === 'action permission') await fixture.refresh({ approve: { allowed: false, reason: null } });
  else if (change === 'missing hash') await fixture.refresh({ plan_hash: null });
  else await fixture.refresh({ env: 'other' });
  const confirm = screen.getByRole('button', { name: 'Confirm approve' });
  await waitFor(() => expect((confirm as HTMLButtonElement).disabled).toBe(true));
  expect((screen.getByRole('checkbox') as HTMLInputElement).disabled).toBe(true);
  await user.click(confirm);
  expect(fixture.posts).toEqual([]);
});

it('preserves acknowledgement for the same review after a routine refresh', async () => {
  const fixture = open();
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Approve' }));
  await user.click(screen.getByRole('checkbox'));
  await fixture.refresh(structuredClone(original));
  expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true);
  await user.click(screen.getByRole('button', { name: 'Confirm approve' }));
  await waitFor(() => expect(fixture.posts).toHaveLength(1));
  expect(fixture.posts[0].body).toEqual({ plan_hash: original.plan_hash });
});

it('requires selecting and acknowledging a newly published promotion commit', async () => {
  const fixture = open();
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Promote' }));
  await user.selectOptions(screen.getByLabelText('Published branch and commit'), `product/main@${original.refs[0].commit}`);
  await user.click(screen.getByRole('checkbox'));
  const commit = 'd'.repeat(40);
  await fixture.refresh({ refs: [{ source: 'product', branch: 'main', commit }] });
  await waitFor(() => expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false));
  const confirm = screen.getByRole('button', { name: 'Confirm promote' });
  expect((confirm as HTMLButtonElement).disabled).toBe(true);
  await user.click(confirm);
  expect(fixture.posts).toEqual([]);
  await user.selectOptions(screen.getByLabelText('Published branch and commit'), `product/main@${commit}`);
  await user.click(screen.getByRole('checkbox'));
  await user.click(confirm);
  await waitFor(() => expect(fixture.posts).toHaveLength(1));
  expect(fixture.posts[0]).toEqual({ path: '/api/v1/composition/environments/preview/promotions', body: { branch: 'main', commit } });
});

it('requires reviewing a changed stopped instance and refuses wake after it resumes', async () => {
  const fixture = open();
  fixture.state('stopped');
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Wake' }));
  await user.click(screen.getByRole('checkbox'));
  await fixture.refresh({ stopped_since: '2026-10-02T10:00:00Z' });
  await waitFor(() => expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false));
  const confirm = screen.getByRole('button', { name: 'Confirm wake' });
  expect((confirm as HTMLButtonElement).disabled).toBe(true);
  await user.click(screen.getByRole('checkbox'));
  fixture.state('deployed');
  expect((confirm as HTMLButtonElement).disabled).toBe(true);
  await user.click(confirm);
  expect(fixture.posts).toEqual([]);
  fixture.state('stopped');
  // Re-open the review to acknowledge the currently reported stopped instance.
  await user.click(screen.getByRole('button', { name: 'Wake' }));
  await user.click(screen.getByRole('checkbox'));
  await user.click(screen.getByRole('button', { name: 'Confirm wake' }));
  await waitFor(() => expect(fixture.posts).toHaveLength(1));
  expect(fixture.posts[0]).toEqual({ path: '/api/v1/composition/environments/preview/wake', body: null });
});
