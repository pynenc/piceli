import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { GitOps } from './Control';

const initial = { branch: 'feature', namespace: 'shop-feature', state: 'approval-required', commit: 'a'.repeat(40), deployed_commit: 'b'.repeat(40), plan_hash: `sha256:${'c'.repeat(64)}` };

function open() {
  const posts: { path: string; body: unknown }[] = [];
  let environment = { ...initial };
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    if (request.method === 'POST') {
      posts.push({ path: new URL(request.url).pathname, body: await request.json() });
      return Response.json({ state: 'requested' });
    }
    return Response.json({ configured: true, controller: { state: 'running' }, envs: [environment] });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><GitOps canChange /></MemoryRouter></QueryClientProvider>);
  return { posts, replace: (change: Partial<typeof initial>) => { environment = { ...environment, ...change }; } };
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each(['plan', 'deployed commit', 'source commit'] as const)('requires a new acknowledgement when polling replaces the reviewed %s', async identity => {
  const { posts, replace } = open();
  const user = userEvent.setup();
  const checkbox = await screen.findByRole('checkbox', { name: 'I reviewed this branch, commit and pending plan.' });
  await user.click(checkbox);
  const approve = screen.getByRole('button', { name: 'Approve pending plan' });
  const promote = screen.getByRole('button', { name: 'Request promotion to main' });
  expect((approve as HTMLButtonElement).disabled).toBe(false);
  expect((promote as HTMLButtonElement).disabled).toBe(false);
  const nextHash = `sha256:${'d'.repeat(64)}`;
  const nextCommit = 'e'.repeat(40);
  replace(identity === 'plan' ? { plan_hash: nextHash } : identity === 'deployed commit' ? { deployed_commit: nextCommit } : { commit: nextCommit });
  await user.click(screen.getByRole('button', { name: 'Refresh' }));
  await waitFor(() => expect((checkbox as HTMLInputElement).checked).toBe(false));
  expect((approve as HTMLButtonElement).disabled).toBe(true);
  expect((promote as HTMLButtonElement).disabled).toBe(true);
  await user.click(approve);
  await user.click(promote);
  expect(posts).toEqual([]);
  await user.click(checkbox);
  await user.click(identity === 'plan' ? approve : promote);
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0]).toEqual(identity === 'plan'
    ? { path: '/api/v1/gitops/approvals', body: { branch: initial.branch, plan_hash: nextHash } }
    : { path: '/api/v1/gitops/promotions', body: { branch: initial.branch, commit: identity === 'deployed commit' ? nextCommit : initial.deployed_commit } });
});
