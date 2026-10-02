import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { Pipeline } from './Pipeline';

const plan = { id: 'plan-1', digest: 'a'.repeat(64), materialized: true, phase: 'final', expires_at: '2099-01-01T00:00:00Z', target: { name: 'test-cluster', namespace: 'shop' }, stages: [{ name: 'apply', action: 'apply' }] };
function open(expires = plan.expires_at, refuse = false) {
  const posts: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    if (request.method === 'POST') {
      posts.push(path);
      if (refuse) return Response.json({ code: 'ui-plan-changed', message: 'Pipeline admission failed.', correlation_id: 'test' }, { status: 409 });
    }
    if (path.endsWith('/plans/plan-1')) return Response.json({ ...plan, expires_at: expires });
    return Response.json({ items: [] });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/pipeline?plan=plan-1']}><Pipeline /></MemoryRouter></QueryClientProvider>);
  return { client, posts };
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('requires a new acknowledgement when the materialized pipeline digest changes', async () => {
  const { client, posts } = open();
  await userEvent.setup().click(await screen.findByRole('checkbox'));
  expect((screen.getByRole('button', { name: 'Approve and deploy' }) as HTMLButtonElement).disabled).toBe(false);
  await act(async () => { client.setQueryData(['pipeline-plan', plan.id], { ...plan, digest: 'b'.repeat(64) }); });
  await waitFor(() => expect((screen.getByRole('button', { name: 'Approve and deploy' }) as HTMLButtonElement).disabled).toBe(true));
  expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false);
  expect(posts).toEqual([]);
});

it('refuses a pipeline plan whose expiry cannot be read', async () => {
  const { posts } = open('invalid');
  expect(await screen.findByText('Plan expired')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Approve and deploy' }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByRole('checkbox')).toBeNull();
  expect(posts).toEqual([]);
});

it.each(['unchecked', 'expired', 'changed digest'])('keeps failed pipeline admission guarded after %s', async state => {
  const { posts, client } = open(plan.expires_at, true);
  const user = userEvent.setup();
  await user.click(await screen.findByRole('checkbox'));
  await user.click(screen.getByRole('button', { name: 'Approve and deploy' }));
  await screen.findByText('Pipeline admission failed.');
  expect(posts).toHaveLength(1);
  if (state === 'unchecked') await user.click(screen.getByRole('checkbox'));
  else await act(async () => { client.setQueryData(['pipeline-plan', plan.id], { ...plan, ...(state === 'expired' ? { expires_at: '2000-01-01T00:00:00Z' } : { digest: 'd'.repeat(64) }) }); });
  await waitFor(() => expect((screen.getByRole('button', { name: 'Approve and deploy' }) as HTMLButtonElement).disabled).toBe(true));
  const approve = screen.getByRole('button', { name: 'Approve and deploy' });
  expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
  await user.click(approve);
  expect(posts).toHaveLength(1);
});
