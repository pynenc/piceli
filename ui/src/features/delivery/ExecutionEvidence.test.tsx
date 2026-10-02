import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import type { Operation, PlanRecord, ResourceIdentity } from '../../api/generated';
import { PlanReview } from './Review';
import { RunView } from './RunView';

const settings: ResourceIdentity = { target_id: 'target', api_version: 'v1', kind: 'ConfigMap', namespace: 'shop', name: 'settings' };
const service: ResourceIdentity = { target_id: 'target', api_version: 'v1', kind: 'Service', namespace: 'shop', name: 'api' };
const deployment: ResourceIdentity = { target_id: 'target', api_version: 'apps/v1', kind: 'Deployment', namespace: 'shop', name: 'api' };
const base: PlanRecord = { id: 'plan-1', application_id: 'shop', digest: 'a'.repeat(64), target: { id: 'target', name: 'cluster', namespace: 'shop' }, intent: 'deploy', release: 'release', expires_at: '2099-01-01T00:00:00Z', summary: { apply: 3 }, diffs: [] };
const plan = { ...base, steps: [
  { ordinal: 0, level: 0, resource: settings, operation: 'apply', dependencies: [] },
  { ordinal: 1, level: 1, resource: deployment, operation: 'apply', dependencies: [settings] },
  { ordinal: 2, level: 1, resource: service, operation: 'apply', dependencies: [settings] },
] };
const runBase: Operation = { id: 'run-1', application_id: 'shop', plan_id: plan.id, approved_digest: plan.digest, actor: 'operator', trigger: 'ui', state: 'failed', created_at: '2026-10-02T10:00:00Z', updated_at: '2026-10-02T10:01:00Z', engine_execution_id: 'engine-1', stages: [{ name: 'deploy', state: 'failed', reason: 'Readiness did not complete' }], capabilities: { resume: { allowed: false }, cancel: { allowed: false } } };
const journal = { execution_id: 'engine-1', state: 'failed', actions: [
  { ordinal: 0, resource: settings, operation: 'apply', state: 'ready', written_at: '2026-10-02T10:00:01Z' },
  { ordinal: 1, resource: deployment, operation: 'apply', state: 'applied', written_at: '2026-10-02T10:00:02Z' },
  { ordinal: 2, resource: service, operation: 'apply', state: 'pending', written_at: null },
], events: [{ sequence: 8, ordinal: 0, state: 'intent' }, { sequence: 9, ordinal: 0, state: 'ready' }, { sequence: 10, ordinal: 1, state: 'applied' }, { sequence: 11, ordinal: null, state: 'error:readiness-timeout' }], logs: [{ resource: deployment, pod: 'api-123', container: 'web', lines: ['Booting application', 'connection refused', '<script>not executable</script>'] }], truncated: false };
afterEach(cleanup);

it('shows real dependency phases and planned action details without bypassing exact-plan approval', async () => {
  const approve = vi.fn();
  render(<MemoryRouter><PlanReview plan={plan} pending={false} allowed onApprove={approve} /></MemoryRouter>);
  const flow = within(screen.getByRole('region', { name: 'Execution order' }));
  expect(flow.getByRole('heading', { name: 'Phase 1' })).toBeTruthy();
  expect(flow.getByRole('heading', { name: 'Phase 2' })).toBeTruthy();
  expect(screen.getByRole('link', { name: 'Compare revision' }).getAttribute('href')).toBe('/applications/shop/activity?history=revisions&compareTo=plan-1');
  expect(flow.getAllByRole('button', { name: /^Inspect planned action/ }).map(button => button.getAttribute('aria-label'))).toEqual(['Inspect planned action 1: ConfigMap settings', 'Inspect planned action 2: Deployment api', 'Inspect planned action 3: Service api']);
  await userEvent.setup().click(flow.getByRole('button', { name: 'Inspect planned action 2: Deployment api' }));
  const details = within(flow.getByRole('region', { name: 'Selected action details' }));
  expect(details.getByText('Depends on')).toBeTruthy();
  expect(details.getByText('ConfigMap / shop / settings')).toBeTruthy();
  const deploy = screen.getByRole('button', { name: 'Deploy to cluster / shop' });
  expect((deploy as HTMLButtonElement).disabled).toBe(true);
  expect(approve).not.toHaveBeenCalled();
  await userEvent.setup().click(screen.getByRole('checkbox'));
  await userEvent.setup().click(deploy);
  expect(approve).toHaveBeenCalledTimes(1);
});

it('shows recorded resource states separately from readiness and exposes real transitions and captured logs', async () => {
  render(<MemoryRouter><RunView operation={{ ...runBase, journal }} plan={plan} pending={false} onResume={vi.fn()} onCancel={vi.fn()} /></MemoryRouter>);
  const flow = within(screen.getByRole('region', { name: 'Execution order' }));
  expect(flow.getByText('1 ready')).toBeTruthy();
  expect(flow.getByText('1 applied')).toBeTruthy();
  expect(flow.getByText('1 pending')).toBeTruthy();
  expect(within(flow.getByRole('button', { name: 'Inspect recorded action 2: Deployment api' })).getByText('Applied')).toBeTruthy();
  const evidence = within(screen.getByRole('region', { name: 'Execution journal and logs' }));
  expect(evidence.getByText('error:readiness-timeout')).toBeTruthy();
  expect(evidence.getByText('No event timestamps were recorded.')).toBeTruthy();
  await userEvent.setup().click(evidence.getByRole('button', { name: /Captured logs/ }));
  expect(evidence.getByLabelText('Captured output for api-123 / web').textContent).toBe('Booting application\nconnection refused\n<script>not executable</script>');
  expect(document.querySelector('script')).toBeNull();
  expect(evidence.getByText(/Recorded failure diagnosis/)).toBeTruthy();
  expect(screen.getByRole('link', { name: 'Compare revision' }).getAttribute('href')).toBe('/applications/shop/activity?history=revisions&compareTo=plan-1');
});

it('does not join another plan or engine journal to the current run', () => {
  render(<MemoryRouter><RunView operation={{ ...runBase, journal: { ...journal, execution_id: 'other-engine' } }} plan={{ ...plan, id: 'other-plan', target: { ...plan.target, name: 'wrong-cluster' } }} pending={false} onResume={vi.fn()} onCancel={vi.fn()} /></MemoryRouter>);
  expect(screen.queryByRole('region', { name: 'Execution order' })).toBeNull();
  expect(screen.queryByText(/wrong-cluster/)).toBeNull();
  expect(screen.getByText('Plan evidence does not match this operation')).toBeTruthy();
  expect(screen.getByText('Journal identity does not match this operation')).toBeTruthy();
  expect(screen.queryByText('error:readiness-timeout')).toBeNull();
  expect(screen.queryByRole('link', { name: 'Compare revision' })).toBeNull();
});

it('does not fabricate resource progress or captured logs for old operations without a journal', () => {
  render(<MemoryRouter><RunView operation={{ ...runBase, state: 'succeeded' }} plan={plan} pending={false} onResume={vi.fn()} onCancel={vi.fn()} /></MemoryRouter>);
  expect(screen.getByText('No resource execution journal is available for this operation.')).toBeTruthy();
  expect(screen.queryByText('3 ready')).toBeNull();
  expect(screen.queryByLabelText(/Captured output/)).toBeNull();
});

it.each(['application_id', 'digest'] as const)('rejects matching plan IDs with a different %s before displaying execution relationships', field => {
  render(<MemoryRouter><RunView operation={{ ...runBase, journal }} plan={{ ...plan, [field]: 'not-the-approved-identity' }} pending={false} onResume={vi.fn()} onCancel={vi.fn()} /></MemoryRouter>);
  expect(screen.queryByRole('region', { name: 'Execution order' })).toBeNull();
  expect(screen.queryByRole('link', { name: 'Compare revision' })).toBeNull();
  expect(screen.getByText('Plan evidence does not match this operation')).toBeTruthy();
  expect(screen.getByRole('region', { name: 'Execution journal and logs' })).toBeTruthy();
});

it.each(['kind', 'namespace', 'target_id', 'api_version', 'name'] as const)('requires complete %s identity to join ordinal receipts with planned resource states', field => {
  const wrongResource = { ...deployment, [field]: 'different' };
  render(<MemoryRouter><RunView operation={{ ...runBase, journal: { ...journal, actions: journal.actions.map(action => action.ordinal === 1 ? { ...action, state: 'ready', resource: wrongResource } : action) } }} plan={plan} pending={false} onResume={vi.fn()} onCancel={vi.fn()} /></MemoryRouter>);
  const flow = within(screen.getByRole('region', { name: 'Execution order' }));
  expect(flow.getByText('1 unreported')).toBeTruthy();
  expect(within(flow.getByRole('button', { name: 'Inspect recorded action 2: Deployment api' })).getByText('Unreported')).toBeTruthy();
  expect(flow.queryByText('2 ready')).toBeNull();
});

it('keeps partial evidence explicit, filters recorded transitions without removing raw evidence and explains absent logs', async () => {
  render(<MemoryRouter><RunView operation={{ ...runBase, journal: { ...journal, logs: [], truncated: true } }} plan={plan} pending={false} onResume={vi.fn()} onCancel={vi.fn()} /></MemoryRouter>);
  const evidence = within(screen.getByRole('region', { name: 'Execution journal and logs' }));
  expect(evidence.getByText('Partial journal evidence')).toBeTruthy();
  await userEvent.setup().type(evidence.getByRole('searchbox', { name: 'Find recorded transition' }), 'readiness-timeout');
  expect(within(evidence.getByRole('list', { name: 'Recorded journal transitions' })).getAllByRole('listitem')).toHaveLength(1);
  expect(evidence.getByLabelText('Raw recorded journal').textContent).toContain('"state": "intent"');
  await userEvent.setup().click(evidence.getByRole('button', { name: /Captured logs/ }));
  expect(evidence.getByText('No captured log output is available. Use the application’s resources view to inspect current workload logs.')).toBeTruthy();
  expect(evidence.queryByLabelText(/Captured output/)).toBeNull();
});

it('shows actions outside dependency levels without inventing another numbered phase', () => {
  const prune = { ordinal: 3, level: null, resource: { ...service, name: 'old-api' }, operation: 'delete', dependencies: [] };
  render(<MemoryRouter><PlanReview plan={{ ...plan, steps: [...plan.steps, prune] }} pending={false} allowed onApprove={vi.fn()} /></MemoryRouter>);
  const flow = within(screen.getByRole('region', { name: 'Execution order' }));
  expect(flow.getByRole('heading', { name: 'Additional actions' })).toBeTruthy();
  expect(flow.queryByRole('heading', { name: 'Phase 3' })).toBeNull();
  expect(flow.getByRole('button', { name: 'Inspect planned action 4: Service old-api' })).toBeTruthy();
});

it('handles omitted optional journal collections and plan dependencies as missing evidence', () => {
  render(<MemoryRouter><RunView operation={{ ...runBase, journal: { execution_id: 'engine-1', state: 'pending' } }} plan={{ ...plan, steps: [{ ordinal: 0, operation: 'apply', resource: settings }] }} pending={false} onResume={vi.fn()} onCancel={vi.fn()} /></MemoryRouter>);
  expect(screen.getByText('No transitions are recorded in this journal.')).toBeTruthy();
  expect(screen.getByText('1 unreported')).toBeTruthy();
  expect(screen.getByText('No resource dependencies are recorded for this action.')).toBeTruthy();
});

it('opens captured logs directly from a bookmarked run URL and preserves the journal view in navigation', async () => {
  render(<MemoryRouter initialEntries={['/runs/run-1?runView=logs#execution-journal']}><RunView operation={{ ...runBase, journal }} plan={plan} pending={false} onResume={vi.fn()} onCancel={vi.fn()} /></MemoryRouter>);
  expect(screen.getByLabelText('Captured output for api-123 / web').textContent).toContain('connection refused');
  const evidence = screen.getByRole('region', { name: 'Execution journal and logs' });
  expect(evidence.id).toBe('execution-journal');
  expect(document.activeElement).toBe(evidence);
  await userEvent.setup().click(within(evidence).getByRole('button', { name: /Journal transitions/ }));
  expect(within(evidence).getByRole('list', { name: 'Recorded journal transitions' })).toBeTruthy();
});
