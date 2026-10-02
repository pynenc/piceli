import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render as renderView, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ReactElement } from 'react';
import userEvent from '@testing-library/user-event';
import type { EvaluationPreview, PlanRecord } from '../../api/generated';
import { PlanReview, PreviewApproval } from './Review';

const render = (element: ReactElement) => renderView(element, { wrapper: MemoryRouter });
const digest = 'a'.repeat(64);
const source = { kind: 'local' as const, revision: 'sha256:source', entrypoint: 'release.py:release' };
const plan: PlanRecord = { id: 'plan-1', application_id: 'shop', digest, target: { id: 'target', name: 'test-cluster', namespace: 'shop' }, source, intent: 'deploy', release: 'release-2', expires_at: '2099-01-01T00:00:00Z', summary: { updates: 1 }, diffs: [] };
const preview: EvaluationPreview = { id: 'preview-1', application_id: 'shop', digest, source, renderer_digest: digest, input_digest: digest, files: ['release.py'], intent: 'deploy', expires_at: plan.expires_at, limits: { max_seconds: 30 } };
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('review approval guards', () => {
  it('keeps every recorded field, unified evidence and comparison limit available', () => {
    const reviewed: PlanRecord = { ...plan, warnings: ['Live state changed'], actions: [{ verb: 'apply' }], checks: { readiness: true }, diffs: [{ resource: { target_id: 'target', api_version: 'apps/v1', kind: 'Deployment', namespace: 'shop', name: 'api' }, operation: 'update', basis: 'server-dry-run', changes: [{ path: 'spec.replicas', op: 'replace', before: 1, after: 2 }, { path: 'metadata.labels.owner', op: 'add', after: 'platform' }, { path: 'metadata.annotations.old', op: 'remove', before: 'legacy' }], unified: '- replicas: 1\n+ replicas: 2', not_compared: ['status'] }] };
    render(<PlanReview plan={reviewed} allowed pending={false} onApprove={vi.fn()} />);
    expect(screen.getByLabelText('Before spec.replicas').textContent).toContain('1');
    expect(screen.getByLabelText('After spec.replicas').textContent).toContain('2');
    expect(screen.getByLabelText('After metadata.labels.owner').textContent).toContain('platform');
    expect(screen.getByLabelText('Before metadata.annotations.old').textContent).toContain('legacy');
    expect(screen.getByText('Fields not compared')).toBeTruthy();
    expect(screen.getByText('status')).toBeTruthy();
    expect(screen.getByText('Unified diff')).toBeTruthy();
    expect(screen.getByText('Actions and checks')).toBeTruthy();
    expect(screen.getByText('Live state changed')).toBeTruthy();
  });
  it.each([
    ['expired plan', { ...plan, expires_at: '2000-01-01T00:00:00Z' }, true, false, 'Plan expired'],
    ['unparseable expiry', { ...plan, expires_at: 'invalid' }, true, false, 'Plan expired'],
    ['unmaterialized pipeline', { ...plan, plan_kind: 'pipeline-preview' }, true, false, 'Materialized plan required'],
    ['missing capability', plan, false, false, 'Deployment unavailable'],
    ['pending admission', plan, true, true, null],
  ] as const)('cannot approve %s even after checking the acknowledgement', async (_name, reviewed, allowed, pending, notice) => {
    const onApprove = vi.fn();
    render(<PlanReview plan={reviewed} allowed={allowed} pending={pending} reason="Scope is read only." onApprove={onApprove} />);
    if (notice) expect(screen.getByText(notice)).toBeTruthy();
    await userEvent.setup().click(screen.getByRole('checkbox'));
    const button = screen.getByRole('button');
    expect((button as HTMLButtonElement).disabled).toBe(true);
    await userEvent.setup().click(button);
    expect(onApprove).not.toHaveBeenCalled();
  });

  it('revokes an acknowledged approval when its plan expires while the page remains open', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-02T12:00:00Z'));
    render(<PlanReview plan={{ ...plan, expires_at: '2026-10-02T12:00:01Z' }} allowed pending={false} onApprove={vi.fn()} />);
    fireEvent.click(screen.getByRole('checkbox'));
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByText('Plan expired')).toBeTruthy();
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(true);
  });

  it('does not authorize source execution from an expired preview', async () => {
    const onApprove = vi.fn();
    render(<PreviewApproval preview={{ ...preview, expires_at: '2000-01-01T00:00:00Z' }} pending={false} onApprove={onApprove} />);
    expect(screen.getByText('Evaluation preview expired')).toBeTruthy();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Approve source evaluation' }));
    expect(onApprove).not.toHaveBeenCalled();
  });

  it.each(['id', 'digest'] as const)('requires a fresh acknowledgement when the plan %s changes in place', async changed => {
    const props = { allowed: true, pending: false, onApprove: vi.fn() };
    const { rerender } = render(<PlanReview plan={plan} {...props} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('checkbox'));
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(false);
    rerender(<PlanReview plan={{ ...plan, [changed]: changed === 'id' ? 'plan-2' : 'b'.repeat(64) }} {...props} />);
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(true);
    await user.click(screen.getByRole('button'));
    expect(props.onApprove).not.toHaveBeenCalled();
  });

  it('keeps rollback scope and data-restoration limits visible before approval', async () => {
    const onApprove = vi.fn();
    render(<PlanReview plan={{ ...plan, intent: 'rollback', release: 'release-1', source: undefined }} allowed pending={false} onApprove={onApprove} />);
    expect(screen.getByText('Rollback is a new deployment')).toBeTruthy();
    expect(screen.getByText(/does not restore application data/)).toBeTruthy();
    expect(screen.getByText('release-1')).toBeTruthy();
    expect(screen.getByText(digest)).toBeTruthy();
    const button = screen.getByRole('button', { name: 'Roll back to test-cluster / shop' });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    const user = userEvent.setup();
    await user.click(screen.getByRole('checkbox'));
    await user.click(button);
    expect(onApprove).toHaveBeenCalledTimes(1);
  });
});
