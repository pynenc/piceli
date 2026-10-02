import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';
import type { Operation, PlanRecord } from '../../api/generated';
import { api } from '../../api/client';
import { RevisionHistory } from './RevisionHistory';

vi.mock('../../api/client', async () => ({ ...await vi.importActual('../../api/client'), api: { plan: vi.fn() } }));
const identity = { target_id: 'cluster-a', api_version: 'apps/v1', kind: 'Deployment', namespace: 'shop', name: 'api' };
const oldPlan = { id: 'plan-old', application_id: 'shop', digest: 'old-digest', target: { id: 'cluster-a', name: 'Cluster A', namespace: 'shop', cluster_uid: 'a' }, source: { kind: 'git', revision: 'a'.repeat(40), entrypoint: 'app.py' }, intent: 'deploy', plan_kind: 'release', authorization: 'manual', release: 'release-old', expires_at: '2020-01-01T00:00:00Z', summary: { update: 1 }, diffs: [], desired_resources_complete: true, desired_resources: [{ resource: identity, manifest: { spec: { replicas: 2, image: 'api:v1' } } }] } satisfies PlanRecord & Record<string, unknown>;
const newPlan = { ...oldPlan, id: 'plan-new', digest: 'new-digest', source: { ...oldPlan.source, revision: 'b'.repeat(40) }, release: 'release-new', desired_resources: [{ resource: identity, manifest: { spec: { replicas: 3, image: 'api:v2' } } }] };
const run = (id: string, plan_id: string, date: string): Operation => ({ id, plan_id, application_id: 'shop', approved_digest: plan_id === 'plan-old' ? 'old-digest' : 'new-digest', actor: 'operator', trigger: 'ui', state: 'succeeded', created_at: date, updated_at: date });
const operations = [run('old', 'plan-old', '2026-10-01T12:00:00Z'), run('new', 'plan-new', '2026-10-02T12:00:00Z'), run('retry', 'plan-new', '2026-10-02T13:00:00Z'), run('cli', '', '2026-09-30T12:00:00Z')];
function Location() { return <output aria-label="Current query">{useLocation().search}</output>; }
function open(path = '/applications/shop/activity?keep=context') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><RevisionHistory applicationId="shop" operations={operations} /><Location /></MemoryRouter></QueryClientProvider>);
}
afterEach(() => { cleanup(); vi.resetAllMocks(); });
function plans() { vi.mocked(api.plan).mockImplementation(async id => (id === oldPlan.id ? oldPlan : newPlan)); }

describe('recorded revision comparison', () => {
  it('compares the two latest distinct plans, retains exact source identities and never renews an old approval', async () => {
    plans(); open();
    expect(await screen.findByRole('region', { name: 'Revision differences' })).toBeTruthy();
    expect(within(screen.getByRole('combobox', { name: 'From revision' })).getAllByRole('option')).toHaveLength(2);
    expect(screen.getByText('a'.repeat(40))).toBeTruthy();
    expect(screen.getByText('b'.repeat(40))).toBeTruthy();
    expect(screen.getByLabelText('Before /spec/image').textContent).toContain('api:v1');
    expect(screen.getByLabelText('After /spec/image').textContent).toContain('api:v2');
    expect(screen.getByRole('link', { name: 'Open From plan' }).getAttribute('href')).toBe('/applications/shop/changes?plan=plan-old');
    expect(screen.queryByRole('button', { name: /\b(?:approve|deploy)\b/i })).toBeNull();
    expect(screen.getByText(/1 operation has no stored plan/)).toBeTruthy();
    expect(vi.mocked(api.plan).mock.calls.map(args => args[0]).sort()).toEqual(['plan-new', 'plan-old']);
  });
  it('swaps comparison direction in the URL without dropping other activity filters', async () => {
    plans(); open('/applications/shop/activity?activity=needs-attention&keep=context');
    await screen.findByRole('region', { name: 'Revision differences' });
    await userEvent.setup().click(screen.getByRole('button', { name: 'Swap revisions' }));
    expect(screen.getByLabelText('Current query').textContent).toContain('compareFrom=plan-new');
    expect(screen.getByLabelText('Current query').textContent).toContain('compareTo=plan-old');
    expect(screen.getByLabelText('Current query').textContent).toContain('activity=needs-attention&keep=context');
    expect(screen.getByLabelText('Before /spec/image').textContent).toContain('api:v2');
  });
  it('does not treat sparse historical diffs as a full revision snapshot', async () => {
    vi.mocked(api.plan).mockImplementation(async id => ({ ...(id === oldPlan.id ? oldPlan : newPlan), desired_resources_complete: false }));
    open();
    expect(await screen.findByText('Revision snapshot unavailable')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Revision differences' })).toBeNull();
  });
  it('excludes masked fields even when a containing object is added or replaced', async () => {
    vi.mocked(api.plan).mockImplementation(async id => ({ ...(id === oldPlan.id ? oldPlan : newPlan), desired_resources: [{ resource: identity, manifest: id === oldPlan.id ? {} : { data: { PASSWORD: 'private-value', public: 'visible' } }, not_compared: ['/data/PASSWORD'] }] }));
    open();
    await screen.findByRole('region', { name: 'Revision differences' });
    expect(screen.queryByLabelText('After /data/PASSWORD')).toBeNull();
    expect(screen.getByLabelText('After /data/public').textContent).toContain('visible');
    expect(screen.getByText('/data/PASSWORD')).toBeTruthy();
  });
  it('shows added and removed empty containers instead of treating them as unchanged', async () => {
    vi.mocked(api.plan).mockImplementation(async id => ({ ...(id === oldPlan.id ? oldPlan : newPlan), desired_resources: [{ resource: identity, manifest: id === oldPlan.id ? {} : { spec: { selector: {}, tolerations: [] } } }] }));
    open();
    await screen.findByRole('region', { name: 'Revision differences' });
    expect(screen.getByLabelText('After /spec/selector').textContent).toBe('{}');
    expect(screen.getByLabelText('After /spec/tolerations').textContent).toBe('[]');
    await userEvent.setup().click(screen.getByRole('button', { name: 'Swap revisions' }));
    expect(screen.getByLabelText('Before /spec/selector').textContent).toBe('{}');
    expect(screen.getByLabelText('Before /spec/tolerations').textContent).toBe('[]');
  });
  it('rejects missing bookmarked plans without silently comparing different revisions', async () => {
    plans(); open('/applications/shop/activity?compareFrom=deleted-plan&compareTo=plan-new');
    expect(await screen.findByText('Selected revision is unavailable')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Revision differences' })).toBeNull();
    expect(vi.mocked(api.plan).mock.calls.some(args => args[0] === 'deleted-plan')).toBe(false);
  });
  it('rejects cross-application plan content and mismatched deployment targets', async () => {
    vi.mocked(api.plan).mockImplementation(async id => id === oldPlan.id ? oldPlan : { ...newPlan, application_id: 'other' });
    open();
    expect(await screen.findByText('Plan does not match this history')).toBeTruthy();
    expect(screen.queryByText('b'.repeat(40))).toBeNull();
  });
  it('refuses a recorded plan whose digest differs from the operation approval', async () => {
    vi.mocked(api.plan).mockImplementation(async id => id === oldPlan.id ? oldPlan : { ...newPlan, digest: 'unexpected-digest' });
    open();
    expect(await screen.findByText('Plan does not match this history')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Revision differences' })).toBeNull();
  });
  it('keeps target differences explicit instead of showing every resource as removed and added', async () => {
    vi.mocked(api.plan).mockImplementation(async id => id === oldPlan.id ? oldPlan : { ...newPlan, target: { ...newPlan.target, id: 'cluster-b' } });
    open();
    expect(await screen.findByText('Different deployment targets')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Revision differences' })).toBeNull();
  });
});
