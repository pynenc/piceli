import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { App } from '../../app/App';
import type { Application, Operation, PlanRecord } from '../../api/generated';

const target = { id: 'target', name: 'test-cluster', namespace: 'shop' };
const application: Application = { id: 'shop', name: 'Shop', target, definition_kind: 'release', ownership: 'native', freshness: { state: 'connected' }, capabilities: { activity: { allowed: true }, evaluate: { allowed: false, reason: 'Source evaluation is unavailable.' }, deploy: { allowed: false, reason: 'Read-only session.' }, rollback: { allowed: false } } };
const plan: PlanRecord = { id: 'plan-1', application_id: 'shop', digest: 'a'.repeat(64), target, intent: 'deploy', release: 'release-2', expires_at: '2099-01-01T00:00:00Z', summary: { update: 1 }, diffs: [{ resource: { target_id: target.id, api_version: 'apps/v1', kind: 'Deployment', namespace: target.namespace, name: 'api' }, operation: 'update', basis: 'server-dry-run', changes: [{ path: '/spec/replicas', op: 'replace', before: 1, after: 2 }], unified: '- replicas: 1\n+ replicas: 2' }] };
const operation: Operation = { id: 'run-1', application_id: application.id, plan_id: plan.id, approved_digest: plan.digest, actor: 'operator', trigger: 'cli', state: 'failed', created_at: '2026-10-01T10:00:00Z', updated_at: '2026-10-01T10:01:00Z' };

function open(path: string, app = application, record = plan) {
  const requests: { path: string; method: string }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const pathname = new URL(request.url).pathname;
    requests.push({ path: pathname, method: request.method });
    if (pathname.endsWith('/capabilities')) return Response.json({ principal: { id: 'local', name: 'Observer' }, targets: [target], actions: { inspect: { allowed: true } } });
    if (pathname.endsWith('/profiles')) return Response.json({ active: null, profiles: [] });
    if (pathname.endsWith('/applications/shop')) return Response.json(app);
    if (pathname.endsWith('/applications/shop/operations')) return Response.json({ items: [operation], cursor: 'history' });
    if (pathname.endsWith('/operations/run-1')) return Response.json(operation);
    if (pathname.endsWith('/plans/plan-1')) return Response.json(record);
    return Response.json({ code: 'not-found', message: 'Not part of this fixture.', correlation_id: 'test' }, { status: 404 });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>);
  return requests;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each(['activity', 'run', 'bookmark'] as const)('opens recorded plan evidence from %s without source evaluation or deployment authority', async entry => {
  const requests = open(entry === 'activity' ? '/applications/shop/activity' : entry === 'run' ? '/runs/run-1' : '/applications/shop/changes?plan=plan-1');
  const user = userEvent.setup();
  if (entry !== 'bookmark') await user.click(await screen.findByRole('link', { name: entry === 'activity' ? 'Review plan' : 'View exact plan' }));
  expect(await screen.findByRole('heading', { name: 'Review deployment' })).toBeTruthy();
  expect(screen.getByLabelText('Before /spec/replicas').textContent).toBe('1');
  expect(screen.getByLabelText('After /spec/replicas').textContent).toBe('2');
  expect(screen.queryByRole('button', { name: 'Prepare a new review' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Prepare source preview' })).toBeNull();
  const changes = within(screen.getByRole('navigation', { name: 'Application views' })).getByRole('link', { name: 'Changes' });
  expect(changes.getAttribute('href')).toBe('/applications/shop/changes?plan=plan-1');
  expect(changes.getAttribute('aria-current')).toBe('page');
  await user.click(changes);
  expect(screen.getByRole('heading', { name: 'Review deployment' })).toBeTruthy();
  await user.click(screen.getByRole('checkbox'));
  const deploy = screen.getByRole('button', { name: 'Deploy to test-cluster / shop' });
  expect((deploy as HTMLButtonElement).disabled).toBe(true);
  await user.click(deploy);
  expect(requests.some(request => request.path.endsWith('/plans/plan-1'))).toBe(true);
  expect(requests.every(request => request.method === 'GET')).toBe(true);
});

it.each(['/applications/shop/changes', '/applications/shop/changes?plan='])('keeps unscoped preparation unavailable at %s in a history-only session', async path => {
  const requests = open(path);
  expect(await screen.findByText('Changes unavailable')).toBeTruthy();
  expect(within(screen.getByRole('navigation', { name: 'Application views' })).queryByRole('link', { name: 'Changes' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Prepare source preview' })).toBeNull();
  expect(requests.some(request => request.path.includes('/plans/'))).toBe(false);
});

it('does not read explicit plans without evaluation or history access', async () => {
  const requests = open('/applications/shop/changes?plan=plan-1', { ...application, capabilities: { ...application.capabilities, activity: { allowed: false } } });
  expect(await screen.findByText('Changes unavailable')).toBeTruthy();
  expect(requests.some(request => request.path.includes('/plans/'))).toBe(false);
});

it('does not present another application’s plan as evidence for the current scope', async () => {
  const requests = open('/applications/shop/changes?plan=plan-1', { ...application, capabilities: { ...application.capabilities, evaluate: { allowed: true }, deploy: { allowed: true } } }, { ...plan, application_id: 'other-app' });
  expect(await screen.findByText('Plan belongs to another application')).toBeTruthy();
  expect(screen.queryByRole('heading', { name: 'Review deployment' })).toBeNull();
  expect(screen.queryByRole('button', { name: /^Deploy to / })).toBeNull();
  expect(screen.queryByLabelText('Before /spec/replicas')).toBeNull();
  expect(requests.every(request => request.method === 'GET')).toBe(true);
});
