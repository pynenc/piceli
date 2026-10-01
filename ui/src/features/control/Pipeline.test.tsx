import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { App } from '../../app/App';

const hash = 'a'.repeat(64);
const base = {
  id: 'plan-1', digest: hash, target: { name: 'fake', namespace: 'shop' },
  expires_at: '2099-01-01T00:00:00Z', materialized: true, phase: 'final',
  stages: [
    { name: 'prerollout', state: 'planned', checks: [{ workload: 'web', kind: 'config' }] },
    { name: 'backup', action: 'backup', claims: [{ claim: 'db-data' }] },
    { name: 'plan', state: 'planned', summary: { create: 1 } },
    { name: 'apply', action: 'apply' },
  ],
};
const capabilities = { principal: { id: 'local', name: 'Operator' }, targets: [], actions: { pipeline: { allowed: true } } };

function open(plan: typeof base, second?: typeof base) {
  const posts: Array<{ path: string; body: unknown }> = [];
  document.cookie = 'piceli_csrf_this=test-csrf; path=/';
  const meta = document.createElement('meta'); meta.name = 'piceli-csrf-cookie'; meta.content = 'piceli_csrf_this'; document.head.append(meta);
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    if (request.method === 'POST') {
      posts.push({ path, body: await request.text() });
      if (path.endsWith('/plans')) return Response.json(plan);
      return Response.json({ id: 'run-1', state: 'queued', plan_id: plan.id, stages: {}, created_at: '2026-09-30T00:00:00Z' }, { status: 202 });
    }
    if (path.endsWith('/capabilities')) return Response.json(capabilities);
    if (path.endsWith('/plans/plan-1')) return Response.json(plan);
    if (path.endsWith('/plans/plan-2')) return Response.json(second);
    if (path.endsWith('/operations/run-1')) return Response.json({ id: 'run-1', state: second ? 'awaiting-review' : 'succeeded', plan_id: plan.id, next_plan_id: second?.id, stages: { prerollout: 'done', backup: 'done', apply: 'done' }, created_at: '2026-09-30T00:00:00Z' });
    if (path.endsWith('/operations')) return Response.json({ items: [] });
    return Response.json({});
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/pipeline']}><App /></MemoryRouter></QueryClientProvider>);
  return posts;
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); document.cookie = 'piceli_csrf_this=; Max-Age=0; path=/'; document.querySelector('meta[name="piceli-csrf-cookie"]')?.remove(); });

it('shows checks and restore points and requires an exact-plan confirmation', async () => {
  const posts = open(base);
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Prepare new plan' }));
  expect(await screen.findByText('Pre-rollout checks')).toBeTruthy();
  expect(screen.getByText('Restore point')).toBeTruthy();
  const approve = screen.getByRole('button', { name: 'Approve and deploy' }) as HTMLButtonElement;
  expect(approve.disabled).toBe(true);
  await user.click(screen.getByRole('checkbox'));
  await user.click(approve);
  expect(await screen.findByRole('heading', { name: 'Pipeline run' })).toBeTruthy();
  const admission = posts.find(item => item.path.endsWith('/pipeline/operations'));
  expect(JSON.parse(admission?.body as string)).toMatchObject({ plan_id: base.id, approved_digest: hash });
});

it('requires approval before starting preliminary build and delivery', async () => {
  const posts = open({ ...base, materialized: false, phase: 'preliminary' });
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Prepare new plan' }));
  expect(await screen.findByText('First approval: build and deliver')).toBeTruthy();
  const button = screen.getByRole('button', { name: 'Approve build and delivery' }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  await userEvent.setup().click(screen.getByRole('checkbox'));
  await userEvent.setup().click(button);
  expect(posts.some(item => item.path.endsWith('/pipeline/operations'))).toBe(true);
});

it('requires a second review of the materialized hash before rollout', async () => {
  const second = { ...base, id: 'plan-2', digest: 'b'.repeat(64) };
  const posts = open({ ...base, materialized: false, phase: 'preliminary' }, second);
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Prepare new plan' }));
  await user.click(screen.getByRole('checkbox'));
  await user.click(screen.getByRole('button', { name: 'Approve build and delivery' }));
  expect(await screen.findByText('Second approval · materialized plan')).toBeTruthy();
  expect(screen.getByText(second.digest)).toBeTruthy();
  const approve = screen.getByRole('button', { name: 'Approve rollout' }) as HTMLButtonElement;
  expect(approve.disabled).toBe(true);
  await user.click(screen.getByRole('checkbox'));
  await user.click(approve);
  expect(JSON.parse(posts.find(item => item.path.endsWith('/operations/run-1/approve'))?.body as string)).toMatchObject({ plan_id: second.id, approved_digest: second.digest });
});
