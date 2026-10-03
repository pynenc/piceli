import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { ClusterBuild } from './ClusterBuild';

const plan = { id: 'build-plan', digest: 'a'.repeat(64), commit: 'b'.repeat(40), cache_key: 'main', expires_at: '2099-01-01T00:00:00Z', preview: { namespace: 'builds', job: 'builder-1', cache_claim: 'cache-main', node_facts: {} } };
const run = { id: 'build-run', state: 'succeeded', actor: 'operator', approved_digest: plan.digest, created_at: '2026-10-02T12:00:00Z', images: { api: 'registry.example/api@sha256:result' } };
function Location() { const location = useLocation(); return <output aria-label="Current address">{location.pathname}{location.search}</output>; }
function open(path = '/cluster-build', expired: boolean | string = false, refuse = false) {
  const posts: { path: string; body: Record<string, unknown> }[] = [];
  const currentPlan = { ...plan, expires_at: typeof expired === 'string' ? expired : expired ? '2000-01-01T00:00:00Z' : plan.expires_at };
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    if (request.method === 'POST') {
      posts.push({ path, body: await request.json() });
      if (refuse) return Response.json({ code: 'ui-plan-changed', message: 'Build admission failed.', correlation_id: 'test' }, { status: 409 });
      return Response.json(path.endsWith('/plans') ? currentPlan : run);
    }
    if (path.endsWith('/plans/build-plan')) return Response.json(currentPlan);
    if (path.endsWith('/operations/build-run')) return Response.json(run);
    if (path.endsWith('/operations')) return Response.json({ items: [run] });
    return new Response(null, { status: 404 });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><ClusterBuild /><Location /></MemoryRouter></QueryClientProvider>);
  return { posts, client };
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); });

it('requires a full commit and cache key, then reviews the exact build before admitting it', async () => {
  const { posts } = open();
  const user = userEvent.setup();
  const prepare = screen.getByRole('button', { name: 'Prepare build plan' });
  await user.type(screen.getByRole('textbox', { name: 'Full Git commit' }), 'abcd');
  await user.type(screen.getByRole('textbox', { name: 'Branch cache key' }), 'main');
  expect((prepare as HTMLButtonElement).disabled).toBe(true);
  await user.clear(screen.getByRole('textbox', { name: 'Full Git commit' }));
  await user.type(screen.getByRole('textbox', { name: 'Full Git commit' }), plan.commit);
  await user.click(prepare);
  await screen.findByRole('heading', { name: 'Review cluster build' });
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toEqual({ commit: plan.commit, cache_key: 'main' });
  expect(screen.getByText(plan.digest)).toBeTruthy();
  expect(screen.getByText('builder-1')).toBeTruthy();
  expect(screen.getByText('cache-main')).toBeTruthy();
  expect(screen.getByLabelText('Current address').textContent).toBe('/cluster-build?plan=build-plan');
  const approve = screen.getByRole('button', { name: 'Approve cluster build' });
  expect((approve as HTMLButtonElement).disabled).toBe(true);
  await user.click(screen.getByRole('checkbox'));
  await user.click(approve);
  await screen.findByRole('heading', { name: 'Build run' });
  expect(posts).toHaveLength(2);
  expect(posts[1].body).toMatchObject({ plan_id: plan.id, approved_digest: plan.digest });
  expect(posts[1].body.idempotency_key).toBeTruthy();
  expect(screen.getByLabelText('Current address').textContent).toBe('/cluster-build?run=build-run');
});

it('can reopen a build plan but never approve one that has expired', async () => {
  const { posts } = open('/cluster-build?plan=build-plan', true);
  expect(await screen.findByText('Plan expired')).toBeTruthy();
  expect(screen.queryByRole('checkbox')).toBeNull();
  const approve = screen.getByRole('button', { name: 'Approve cluster build' });
  expect((approve as HTMLButtonElement).disabled).toBe(true);
  await userEvent.setup().click(approve);
  expect(posts).toEqual([]);
});

it('opens a historical build through its durable link without admitting another build', async () => {
  const { posts } = open();
  const history = await screen.findByRole('link', { name: /2026/ });
  expect(history.getAttribute('href')).toBe('/cluster-build?run=build-run');
  await userEvent.setup().click(history);
  await screen.findByRole('heading', { name: 'Build run' });
  expect(screen.getByText(/registry.example\/api@sha256:result/)).toBeTruthy();
  expect(screen.getByLabelText('Current address').textContent).toBe('/cluster-build?run=build-run');
  expect(posts).toEqual([]);
});

it('requires a new acknowledgement if a refreshed build plan has a different digest', async () => {
  const { posts, client } = open('/cluster-build?plan=build-plan');
  const user = userEvent.setup();
  await user.click(await screen.findByRole('checkbox'));
  const approve = screen.getByRole('button', { name: 'Approve cluster build' });
  expect((approve as HTMLButtonElement).disabled).toBe(false);
  await act(async () => { client.setQueryData(['cluster-build-plan', plan.id], { ...plan, digest: 'c'.repeat(64) }); });
  await waitFor(() => expect((screen.getByRole('button', { name: 'Approve cluster build' }) as HTMLButtonElement).disabled).toBe(true));
  expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false);
  expect(posts).toHaveLength(0);
});

it('refuses a build plan whose expiry cannot be read', async () => {
  const { posts } = open('/cluster-build?plan=build-plan', 'invalid');
  expect(await screen.findByText('Plan expired')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Approve cluster build' }) as HTMLButtonElement).disabled).toBe(true);
  expect(posts).toHaveLength(0);
});

it.each(['unchecked', 'expired', 'changed digest'])('keeps failed build admission guarded after %s', async state => {
  const { posts, client } = open('/cluster-build?plan=build-plan', false, true);
  const user = userEvent.setup();
  await user.click(await screen.findByRole('checkbox'));
  await user.click(screen.getByRole('button', { name: 'Approve cluster build' }));
  await screen.findByText('Build admission failed.');
  expect(posts).toHaveLength(1);
  if (state === 'unchecked') await user.click(screen.getByRole('checkbox'));
  else await act(async () => { client.setQueryData(['cluster-build-plan', plan.id], { ...plan, ...(state === 'expired' ? { expires_at: '2000-01-01T00:00:00Z' } : { digest: 'd'.repeat(64) }) }); });
  await waitFor(() => expect((screen.getByRole('button', { name: 'Approve cluster build' }) as HTMLButtonElement).disabled).toBe(true));
  const approve = screen.getByRole('button', { name: 'Approve cluster build' });
  expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
  await user.click(approve);
  expect(posts).toHaveLength(1);
});
