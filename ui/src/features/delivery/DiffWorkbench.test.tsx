import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import type { PlanRecord, ResourceDiff } from '../../api/generated';
import { DiffWorkbench } from './DiffWorkbench';
import { PlanReview } from './Review';

const resource = { target_id: 'target', api_version: 'apps/v1', kind: 'Deployment', namespace: 'shop', name: 'api' };
const diffs: ResourceDiff[] = [
  { resource, operation: 'update', basis: 'server-dry-run', changes: [{ path: 'spec.replicas', op: 'replace', before: 1, after: 3 }, { path: 'spec.template.image', op: 'replace', before: 'api:old', after: 'api:new' }], unified: '- replicas: 1\n+ replicas: 3', not_compared: ['status'] },
  { resource: { ...resource, kind: 'ConfigMap', name: 'settings' }, operation: 'create', basis: 'client', changes: [{ path: 'data.mode', op: 'add', after: 'fast' }], unified: '+ mode: fast' },
  { resource: { ...resource, kind: 'Service', name: 'legacy' }, operation: 'delete', basis: 'client', changes: [{ path: 'spec.ports', op: 'remove', before: [8080] }], unified: '- port: 8080' },
  { resource: { ...resource, name: 'unchanged' }, operation: 'noop', basis: 'client', changes: [], unified: '' },
];
afterEach(cleanup);

describe('exact-plan change navigation', () => {
  it('categorizes real resource operations and preserves field evidence after selection', async () => {
    render(<DiffWorkbench diffs={diffs} />);
    expect(screen.getByLabelText('Before spec.replicas').textContent).toBe('1');
    expect(screen.getByRole('button', { name: 'Added 1' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Changed 1' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Removed 1' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Other 1' })).toBeTruthy();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Added 1' }));
    expect(screen.getByLabelText('After data.mode').textContent).toContain('fast');
    expect(screen.queryByLabelText('Before data.mode')).toBeNull();
    expect(screen.getByText('Field not present')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Removed 1' }));
    expect(screen.getByLabelText('Before spec.ports').textContent).toContain('8080');
    expect(screen.queryByLabelText('After spec.ports')).toBeNull();
    expect(screen.getByText('Field removed')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Other 1' }));
    expect(screen.getByText('No individual field changes reported.')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    await user.click(within(screen.getByRole('navigation', { name: 'Changed resources' })).getByRole('button', { name: /api Deployment/ }));
    expect(screen.getByText('Fields not compared')).toBeTruthy();
    expect(screen.getByText('Unified diff')).toBeTruthy();
  });

  it('searches exact field values, selects a matching resource, and recovers from no matches', async () => {
    render(<DiffWorkbench diffs={diffs} />);
    const user = userEvent.setup();
    await user.type(screen.getByRole('searchbox', { name: 'Search changes' }), 'api:new');
    expect(screen.getByText('1 of 4 resources')).toBeTruthy();
    expect(screen.getByLabelText('Before spec.template.image').textContent).toContain('api:old');
    expect(screen.queryByLabelText('Before spec.replicas')).toBeNull();
    expect(screen.getByText(/Approval includes all 4 resource entries/)).toBeTruthy();
    await user.clear(screen.getByRole('searchbox'));
    await user.type(screen.getByRole('searchbox'), 'missing-resource');
    expect(screen.getByText('No changes match your filters')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(screen.getByLabelText('After spec.replicas')).toBeTruthy();
  });

  it('does not change the acknowledged exact plan when inspection filters change', async () => {
    const onApprove = vi.fn();
    const plan: PlanRecord = { id: 'plan-1', application_id: 'shop', digest: 'a'.repeat(64), target: { id: 'target', name: 'test-cluster', namespace: 'shop' }, intent: 'deploy', release: 'release-1', expires_at: '2099-01-01T00:00:00Z', summary: { creates: 1, updates: 1, deletes: 1 }, diffs };
    const { rerender } = render(<PlanReview plan={plan} allowed pending={false} onApprove={onApprove} />, { wrapper: MemoryRouter });
    const user = userEvent.setup();
    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: 'Removed 1' }));
    await user.click(screen.getByRole('button', { name: 'Deploy to test-cluster / shop' }));
    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(screen.getByText(plan.digest)).toBeTruthy();
    rerender(<PlanReview plan={{ ...plan, digest: 'b'.repeat(64) }} allowed pending={false} onApprove={onApprove} />);
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole('button', { name: 'Deploy to test-cluster / shop' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('button', { name: 'All resources 4' }).getAttribute('aria-pressed')).toBe('true');
  });
});
