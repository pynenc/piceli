import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import type { Operation } from '../../api/generated';
import { OperationTimeline } from './OperationTimeline';

const base: Operation = { id: 'run-first', application_id: 'shop/api', plan_id: 'plan/1', approved_digest: 'a'.repeat(64), actor: 'operator', trigger: 'ui', state: 'succeeded', created_at: '2026-09-29T12:00:00Z', updated_at: '2026-09-29T12:01:00Z' };
const operations: Operation[] = [base, { ...base, id: 'run-failed', state: 'failed', error_code: 'readiness-timeout', created_at: '2026-09-30T12:00:00Z', stages: [{ name: 'readiness', state: 'failed', reason: 'Deadline exceeded' }] }, { ...base, id: 'run-recovered', created_at: '2026-10-01T12:00:00Z', attempt: 2, recovery_of: 'run-failed', checks_outcome: 'failed', engine_release: 'release-2' }];
function Location() { const location = useLocation(); return <output aria-label="Current query">{location.search}</output>; }
function open(path = '/applications/shop/activity') { render(<MemoryRouter initialEntries={[path]}><OperationTimeline operations={operations} /><Location /></MemoryRouter>); }
afterEach(cleanup);

describe('recorded deployment activity', () => {
  it('orders actual runs by timestamp and links exact plans, resources and recovery', () => {
    open();
    expect(screen.getAllByRole('article').map(element => element.getAttribute('aria-label'))).toEqual(['Run run-recovered', 'Run run-failed', 'Run run-first']);
    const recovered = within(screen.getByRole('article', { name: 'Run run-recovered' }));
    expect(recovered.getByRole('link', { name: 'Review plan' }).getAttribute('href')).toBe('/applications/shop%2Fapi/changes?plan=plan%2F1');
    expect(recovered.getByRole('link', { name: 'Inspect resources' }).getAttribute('href')).toBe('/applications/shop%2Fapi/resources');
    expect(recovered.getByRole('link', { name: 'Recovery of run-failed' }).getAttribute('href')).toBe('/runs/run-failed');
    expect(screen.getByText('readiness-timeout')).toBeTruthy();
    expect(screen.getByText('1 execution stage')).toBeTruthy();
  });

  it('includes failed checks in attention filters and preserves unrelated URL state', async () => {
    open('/applications/shop/activity?keep=context');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Needs attention 2' }));
    expect(screen.getAllByRole('article')).toHaveLength(2);
    expect(screen.queryByRole('article', { name: 'Run run-first' })).toBeNull();
    expect(screen.getByLabelText('Current query').textContent).toBe('?keep=context&activity=needs-attention');
    await user.type(screen.getByRole('searchbox', { name: 'Search runs' }), 'release-2');
    expect(screen.getAllByRole('article')).toHaveLength(1);
    expect(screen.getByRole('article', { name: 'Run run-recovered' })).toBeTruthy();
    expect(screen.getByLabelText('Current query').textContent).toContain('activitySearch=release-2');
    await user.clear(screen.getByRole('searchbox'));
    await user.click(screen.getByRole('button', { name: 'All runs 3' }));
    expect(screen.getAllByRole('article')).toHaveLength(3);
    expect(screen.getByLabelText('Current query').textContent).toBe('?keep=context');
  });

  it('restores bookmarked filters without hiding the count of all recorded operations', () => {
    open('/applications/shop/activity?activity=needs-attention&activitySearch=missing');
    expect(screen.getByRole('button', { name: 'Needs attention 2' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByText('No runs match these filters')).toBeTruthy();
    expect(screen.getByText(/0 of 3 recorded operations/)).toBeTruthy();
  });

  it('offers a direct recorded-log destination and restores compact history sorting in the URL', async () => {
    open('/applications/shop/activity?keep=context&activitySort=oldest');
    expect(screen.getAllByRole('article').map(element => element.getAttribute('aria-label'))).toEqual(['Run run-first', 'Run run-failed', 'Run run-recovered']);
    const failed = within(screen.getByRole('article', { name: 'Run run-failed' }));
    expect(failed.getByRole('link', { name: 'Open logs' }).getAttribute('href')).toBe('/runs/run-failed?runView=logs#execution-journal');
    await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'Sort runs' }), 'newest');
    expect(screen.getAllByRole('article')[0].getAttribute('aria-label')).toBe('Run run-recovered');
    expect(screen.getByLabelText('Current query').textContent).toBe('?keep=context');
  });
});
