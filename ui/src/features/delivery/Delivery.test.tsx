import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { App } from '../../app/App';
import type { Application, EvaluationPreview, Operation, PlanRecord } from '../../api/generated';

const digest = 'a'.repeat(64);
const target = { id: 'target', name: 'test-cluster', namespace: 'shop' };
const source = { kind: 'local' as const, revision: 'sha256:source', entrypoint: 'release.py:release' };
const app: Application = { id: 'shop', name: 'shop', target, definition_kind: 'release', ownership: 'native', source, freshness: { state: 'connected' }, capabilities: { evaluate: { allowed: true }, deploy: { allowed: true }, rollback: { allowed: true } } };
const preview: EvaluationPreview = { id: 'preview-1', application_id: 'shop', digest, source, renderer_digest: digest, input_digest: digest, files: ['release.py'], intent: 'deploy', expires_at: '2099-01-01T00:00:00Z', limits: { max_seconds: 30 } };
const plan: PlanRecord = { id: 'plan-1', application_id: 'shop', digest, target, source, intent: 'deploy', release: 'release-2', expires_at: '2099-01-01T00:00:00Z', summary: { updates: 1 }, diffs: [{ resource: { target_id: 'target', api_version: 'apps/v1', kind: 'Deployment', namespace: 'shop', name: 'api' }, operation: 'update', basis: 'server-dry-run', changes: [{ path: 'spec.replicas', op: 'replace', before: 1, after: 2 }], unified: '- replicas: 1\n+ replicas: 2' }] };
const operation: Operation = { id: 'run-1', application_id: 'shop', plan_id: plan.id, approved_digest: digest, actor: 'operator', trigger: 'ui', state: 'succeeded', created_at: '2026-09-29T12:00:00Z', updated_at: '2026-09-29T12:01:00Z', stages: [{ name: 'apply', state: 'succeeded' }, { name: 'checks', state: 'succeeded' }], deployment_outcome: 'succeeded', checks_outcome: 'succeeded', receipts: [{ state: 'succeeded', resource: 'Deployment/api' }] };
let requests: { path: string; body: Record<string, unknown>; csrf: string | null }[];
let failAdmission: boolean;
let currentOperation: Operation;
beforeEach(() => {
  requests = []; failAdmission = false; currentOperation = operation; sessionStorage.clear();
  // Another local UI's CSRF cookie for the same host is visible too; the page names its own.
  document.cookie = 'piceli_csrf_other=other-csrf; path=/'; document.cookie = 'piceli_csrf_this=test-csrf; path=/';
  const meta = document.createElement('meta'); meta.name = 'piceli-csrf-cookie'; meta.content = 'piceli_csrf_this'; document.head.append(meta);
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    if (request.method === 'POST') {
      const body = await request.json(); requests.push({ path, body, csrf: request.headers.get('X-Piceli-CSRF') });
      if (path.endsWith('/evaluation-preview')) return Response.json({ ...preview, intent: body.intent, release: body.release });
      if (path.endsWith('/evaluations')) return Response.json({ id: 'evaluation-1', application_id: 'shop', state: 'queued' }, { status: 202 });
      if (path.endsWith('/resume')) return Response.json({ ...operation, id: 'run-2', recovery_of: 'run-1', attempt: 2 }, { status: 202 });
      if (path.endsWith('/cancel')) return Response.json({ ...currentOperation, state: 'cancelling' }, { status: 202 });
      if (failAdmission) { failAdmission = false; throw new TypeError('network disconnected'); }
      return Response.json(operation, { status: 202 });
    }
    if (path.endsWith('/capabilities')) return Response.json({ principal: { id: 'local', name: 'Operator' }, targets: [target], actions: {} });
    if (path.endsWith('/evaluations/evaluation-1')) return Response.json({ id: 'evaluation-1', application_id: 'shop', state: 'succeeded', plan_id: plan.id });
    if (path.endsWith('/plans/plan-1')) return Response.json(plan);
    if (path.endsWith('/operations/run-1')) return Response.json(currentOperation);
    if (path.endsWith('/operations/run-2')) return Response.json({ ...operation, id: 'run-2', recovery_of: 'run-1', attempt: 2 });
    if (path.endsWith('/releases')) return Response.json({ items: [{ name: 'release-1', state: 'succeeded', capabilities: { rollback: { allowed: true } } }], cursor: '1' });
    if (path.endsWith('/operations')) return Response.json({ items: [currentOperation], cursor: '1' });
    return Response.json(app);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); document.cookie = 'piceli_csrf_other=; Max-Age=0; path=/'; document.cookie = 'piceli_csrf_this=; Max-Age=0; path=/'; document.querySelector('meta[name="piceli-csrf-cookie"]')?.remove(); });
function open(path: string) { const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } }); render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>); }

describe('exact-plan delivery', () => {
  it('requires both approvals and retries the identical request after a lost response', async () => {
    failAdmission = true;
    open('/applications/shop/changes');
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Prepare source preview' }));
    expect(requests).toHaveLength(1);
    await user.click(await screen.findByRole('button', { name: 'Approve source evaluation' }));
    const deploy = await screen.findByRole('button', { name: 'Deploy to test-cluster / shop' });
    expect((deploy as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByLabelText('Before spec.replicas').textContent).toContain('1');
    expect(screen.getByLabelText('After spec.replicas').textContent).toContain('2');
    await user.click(screen.getByRole('checkbox'));
    await user.click(deploy);
    const retry = await screen.findByRole('button', { name: 'Retry same deployment request' });
    await user.click(retry);
    expect(await screen.findByRole('heading', { name: 'Deployment run' })).toBeTruthy();
    const admissions = requests.filter(r => r.path.endsWith('/operations'));
    expect(admissions).toHaveLength(2);
    expect(admissions[0].body).toEqual(admissions[1].body);
    expect(admissions[0].body.approved_digest).toBe(digest);
    expect(admissions[0].body.plan_id).toBe(plan.id);
    expect(requests.every(r => r.csrf === 'test-csrf')).toBe(true);
    expect(requests.filter(r => r.path.endsWith('/evaluations'))[0].body.approved_digest).toBe(preview.digest);
  });
  it('plans rollback from an archived release without declaring it executed', async () => {
    open('/applications/shop/changes?intent=rollback');
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText('Archived release'), 'release-1');
    await user.click(screen.getByRole('button', { name: 'Prepare source preview' }));
    expect(await screen.findByRole('button', { name: 'Approve source evaluation' })).toBeTruthy();
    expect(requests[0].body).toEqual({ intent: 'rollback', release: 'release-1' });
    expect(requests.some(r => r.path.endsWith('/operations'))).toBe(false);
  });
  it('creates a new recovery attempt without relabeling the previous failed operation', async () => {
    currentOperation = { ...operation, state: 'failed', deployment_outcome: 'failed', error_code: 'readiness-timeout', capabilities: { resume: { allowed: true } }, stages: [{ name: 'apply', state: 'succeeded' }, { name: 'readiness', state: 'failed', reason: 'Deadline exceeded' }] };
    open('/runs/run-1');
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Resume as a new attempt' }));
    expect(screen.getByRole('dialog')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Approve new attempt' }));
    await waitFor(() => expect(screen.getByText(/Attempt 2/)).toBeTruthy());
    expect(requests[0].path).toBe('/api/v1/operations/run-1/resume');
    expect(requests[0].body.approved_digest).toBe(digest);
    expect(currentOperation.state).toBe('failed');
  });
  it('cancellation requests do not present rollback or success', async () => {
    currentOperation = { ...operation, state: 'running', capabilities: { cancel: { allowed: true } }, deployment_outcome: 'unknown', checks_outcome: 'unknown' };
    open('/runs/run-1');
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Cancel operation' }));
    await user.click(screen.getByRole('button', { name: 'Request cancellation' }));
    expect(await screen.findByText('Cancellation requested')).toBeTruthy();
    expect(requests[0].path).toBe('/api/v1/operations/run-1/cancel');
    expect(screen.queryByText('Rollback complete')).toBeNull();
  });
});
