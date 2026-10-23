import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { Builds, type BuildsPage } from './Builds';

const DIGEST = `sha256:${'d'.repeat(64)}`;
const page: BuildsPage = {
  configured: true, schema: 'piceli.builds.v1', targets: ['main', 'web-1'],
  stages: ['approval-required', 'queued', 'fetching-inputs', 'building', 'signing', 'uploading', 'published', 'failed', 'cancelled', 'rejected'],
  builds: [
    {
      id: 'web-1@2026-10-24T10:00:00Z', kind: 'nix-publish', target: 'web-1', trigger: { kind: 'cli', by: 'ana', detail: 'cli web-v1.2.0' },
      commit: 'b'.repeat(40), tag: 'web-v1.2.0', branch: null, host: 'mini', stage: 'approval-required', stages: [], log_tail: null,
      requested_at: '2026-10-24T10:00:00Z', started_at: null, finished_at: null, seconds: 30, plan_digest: DIGEST,
      plan: { attribute: '.#web-1', builder: 'mini', manifest: `piceli/web-1/${'b'.repeat(40)}.json`, pointers: ['piceli/web-1/refs/tags/web-v1.2.0.json'] },
      reason: null, images: [], running_on: [], actions: { approve: true, retry: false, cancel: true },
    },
    {
      id: 'images:main@2026-10-24T09:00:00Z', kind: 'images', target: 'main', trigger: { kind: 'push', by: null, detail: 'push shop/main' },
      commit: 'a'.repeat(40), tag: null, branch: null, host: 'laptop', stage: 'building',
      stages: [{ stage: 'queued', started_at: '2026-10-24T09:00:00Z', finished_at: '2026-10-24T09:00:04Z', seconds: 4 }, { stage: 'building', started_at: '2026-10-24T09:00:04Z', finished_at: null, seconds: null }],
      log_tail: 'Compiling web v0.1.0', requested_at: '2026-10-24T09:00:00Z', started_at: '2026-10-24T09:00:04Z', finished_at: null, seconds: 75, plan_digest: null, plan: null,
      reason: null, images: ['web'], running_on: [], actions: { approve: false, retry: false, cancel: true },
    },
    {
      id: 'web-1@2026-10-23T08:00:00Z', kind: 'nix-publish', target: 'web-1', trigger: { kind: 'tag', by: null, detail: 'tag web-v1.1.0' },
      commit: 'c'.repeat(40), tag: 'web-v1.1.0', branch: null, host: 'mini', stage: 'published',
      stages: [{ stage: 'building', started_at: '2026-10-23T08:00:10Z', finished_at: '2026-10-23T08:02:10Z', seconds: 120 }], log_tail: null,
      requested_at: '2026-10-23T08:00:00Z', started_at: '2026-10-23T08:00:10Z', finished_at: '2026-10-23T08:03:00Z', seconds: 180, plan_digest: DIGEST, plan: null,
      reason: null, images: [], running_on: ['web-1'], actions: { approve: false, retry: true, cancel: false },
    },
  ],
};

function open(body: BuildsPage, canChange = true) {
  const calls: { url: string; method: string; body: string | null }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    calls.push({ url: request.url, method: request.method, body: request.method === 'POST' ? await request.text() : null });
    return request.method === 'POST' ? Response.json({ state: 'requested' }, { status: 202 }) : Response.json(body);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter><Builds canChange={canChange} /></MemoryRouter></QueryClientProvider>);
  return calls;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('shows every build with its trigger, commit, host, stages, log and targets', async () => {
  open(page);
  const waiting = await screen.findByRole('region', { name: `Build ${page.builds[0].id}` });
  expect(within(waiting).getByText('cli by ana')).toBeTruthy();
  expect(within(waiting).getByText('mini')).toBeTruthy();
  const running = screen.getByRole('region', { name: `Build ${page.builds[1].id}` });
  expect(within(running).getByLabelText('Log of main').textContent).toContain('Compiling web');
  expect(within(running).getByText('75 s so far')).toBeTruthy();
  const done = screen.getByRole('region', { name: `Build ${page.builds[2].id}` });
  expect(within(done).getByText('web-1', { selector: 'span' })).toBeTruthy();
  expect(within(done).getByRole('button', { name: 'Retry' })).toBeTruthy();
  expect(screen.getByLabelText('Builds state').textContent).toContain('Waiting for approval1');
});

it('approves the exact digest after review, cancels and builds now', async () => {
  const user = userEvent.setup();
  const calls = open(page);
  const waiting = await screen.findByRole('region', { name: `Build ${page.builds[0].id}` });
  await user.click(within(waiting).getByRole('button', { name: 'Approve' }));
  const review = screen.getByRole('region', { name: 'Review plan of web-1' });
  expect(within(review).getByText(DIGEST)).toBeTruthy();
  const confirm = within(review).getByRole('button', { name: 'Confirm approve' }) as HTMLButtonElement;
  expect(confirm.disabled).toBe(true);
  await user.click(within(review).getByRole('checkbox'));
  await user.click(confirm);
  await waitFor(() => expect(calls.some(call => call.url.endsWith('/composition/builds/web-1/approve') && call.body?.includes(DIGEST))).toBe(true));

  await user.click(within(screen.getByRole('region', { name: `Build ${page.builds[1].id}` })).getByRole('button', { name: 'Cancel build' }));
  await waitFor(() => expect(calls.some(call => call.url.endsWith('/composition/builds/main/cancel'))).toBe(true));

  const form = screen.getByRole('region', { name: 'Build now' });
  await user.selectOptions(within(form).getByLabelText('Target'), 'web-1');
  await user.type(within(form).getByLabelText('Tag or commit'), 'web-v1.1.0');
  await user.click(within(form).getByRole('button', { name: 'Build now' }));
  await waitFor(() => expect(calls.some(call => call.method === 'POST' && call.url.endsWith('/composition/builds') && call.body === JSON.stringify({ target: 'web-1', ref: 'web-v1.1.0' }))).toBe(true));
});

it('offers no actions without the deploy capability, and says when no controller runs', async () => {
  open(page, false);
  await screen.findByRole('region', { name: `Build ${page.builds[0].id}` });
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
  expect(screen.queryByRole('region', { name: 'Build now' })).toBeNull();
  cleanup();
  open({ configured: false, schema: 'piceli.builds.v1', builds: [], stages: [] });
  expect(await screen.findByText('No GitOps controller')).toBeTruthy();
});
