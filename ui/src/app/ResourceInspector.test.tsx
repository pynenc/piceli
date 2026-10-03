import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { api } from '../api/client';
import type { Application } from '../api/generated';
import { Resources } from './Resources';

type Resource = Awaited<ReturnType<typeof api.resource>>;

vi.mock('../features/observation/useObservationEvents', () => ({ useObservationEvents: () => ({ connected: true, gap: false }) }));

const application: Application = { id: 'shop', name: 'Shop', target: { id: 'test', name: 'Test cluster', namespace: 'shop' }, definition_kind: 'inventory', ownership: 'inventory', freshness: { state: 'connected' } };
function resource(kind: string, name: string, uid: string, owners: string[] = []): Resource {
  return { id: `id-${uid}`, identity: { target_id: 'test', api_version: 'apps/v1', kind, namespace: 'shop', name, uid }, presence: 'present', ownership: 'unknown', health: 'unknown', relation: 'unknown', owner_uids: owners };
}
const deployment = resource('Deployment', 'api', 'deployment');
const replica = resource('ReplicaSet', 'api-release', 'replica', ['deployment']);
const pod = resource('Pod', 'api-pod', 'pod', ['replica']);
let inventory: Resource[];
let details: Map<string, Resource>;

beforeEach(() => {
  inventory = [deployment, replica, pod];
  details = new Map(inventory.map(item => [item.id, item]));
  vi.spyOn(api, 'resources').mockImplementation(async () => ({ items: inventory, cursor: 'observed', freshness: application.freshness }));
  vi.spyOn(api, 'resource').mockImplementation(async (_application, id) => details.get(id)!);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function Location() { const location = useLocation(); return <output aria-label="Current address">{location.pathname}{location.search}</output>; }
function open(search = '', defaultView?: 'table' | 'relationships') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[`/applications/shop/resources${search}`]}><Resources application={application} defaultView={defaultView} /><Location /></MemoryRouter></QueryClientProvider>);
}
function address() { return new URL(screen.getByLabelText('Current address').textContent!, 'http://localhost'); }

describe('resource context and evidence', () => {
  it('allows an overview graph default while keeping an explicit table choice in the address', async () => {
    open('?filter=api', 'relationships');
    await screen.findByText('Observed ownership');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Table' }));
    expect(screen.queryByText('Observed ownership')).toBeNull();
    expect(address().searchParams.get('view')).toBe('table');
    expect(address().searchParams.get('filter')).toBe('api');
    await user.click(screen.getByRole('button', { name: 'Relationships' }));
    expect(screen.getByText('Observed ownership')).toBeTruthy();
  });

  it.each([
    ['?view=table', 'relationships', false],
    ['?view=relationships', 'table', true],
    ['', 'table', false],
  ] as const)('honors view precedence at %s with default %s', async (search, defaultView, graph) => {
    open(search, defaultView);
    await screen.findByRole('button', { name: 'Inspect Deployment api in shop' });
    expect(Boolean(screen.queryByText('Observed ownership'))).toBe(graph);
  });

  it('navigates exact owner and child UIDs beyond the filter and restores the original trigger on close', async () => {
    inventory.push(resource('ReplicaSet', 'api-release', 'unrelated'));
    open('?view=relationships&filter=Deployment&target=test');
    const user = userEvent.setup();
    const trigger = await screen.findByRole('button', { name: 'Inspect Deployment api in shop' });
    await user.click(trigger);
    const dialog = screen.getByRole('dialog', { name: 'Resource details' });
    const child = await within(dialog).findByRole('link', { name: 'Inspect related ReplicaSet api-release in shop' });
    const destination = new URL(child.getAttribute('href')!, 'http://localhost');
    expect(destination.searchParams.get('resource')).toBe(replica.id);
    expect(destination.searchParams.get('filter')).toBe('Deployment');
    expect(within(dialog).getAllByRole('link', { name: /^Inspect related / })).toHaveLength(1);
    await user.click(child);
    const title = await within(dialog).findByRole('heading', { name: 'api-release' });
    await waitFor(() => expect(document.activeElement).toBe(title));
    expect(address().searchParams.get('resource')).toBe(replica.id);
    expect(address().searchParams.get('panel')).toBe('summary');
    expect(address().searchParams.get('target')).toBe('test');
    const relationships = within(dialog).getByRole('region', { name: 'Related resources' });
    expect(within(relationships).getByRole('link', { name: 'Inspect related Deployment api in shop' })).toBeTruthy();
    expect(within(relationships).getByRole('link', { name: 'Inspect related Pod api-pod in shop' })).toBeTruthy();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(address().searchParams.get('filter')).toBe('Deployment');
    expect(address().searchParams.has('resource')).toBe(false);
  });

  it('keeps unmatched owner references explicit and never substitutes a same-name resource', async () => {
    const orphan = { ...pod, owner_uids: ['missing-owner', 'missing-owner'], identity: { ...pod.identity, uid: null } };
    inventory = [orphan, resource('ReplicaSet', 'api-release', 'different-uid')];
    details.set(orphan.id, orphan);
    open(`?resource=${orphan.id}`);
    const related = await screen.findByRole('region', { name: 'Related resources' });
    expect(within(related).getAllByText('missing-owner')).toHaveLength(1);
    expect(within(related).getByText('Owner outside loaded inventory')).toBeTruthy();
    expect(within(related).queryByRole('link')).toBeNull();
    expect(within(related).getByText('Child relationships need an observed UID.')).toBeTruthy();
    expect(within(related).getByText(/Additional pages or unavailable scopes/)).toBeTruthy();
  });

  it('shows allowed condition evidence without treating True as universally healthy or exposing extra fields', async () => {
    details.set(pod.id, { ...pod, capabilities: { conditions: { allowed: true } }, conditions: [{ type: 'DiskPressure', status: 'True', lastTransitionTime: '2026-10-02T10:00:00Z', message: 'unprojected-private-message', reason: 'unprojected-reason' }, { type: 'Ready', status: 'False' }] });
    open(`?resource=${pod.id}`);
    const evidence = await screen.findByRole('table', { name: 'Reported conditions' });
    expect(within(evidence).getByRole('cell', { name: 'DiskPressure' })).toBeTruthy();
    expect(within(evidence).getByRole('cell', { name: 'True' })).toBeTruthy();
    expect(within(evidence).getByRole('cell', { name: 'False' })).toBeTruthy();
    expect(within(evidence).getByText('2026-10-02T10:00:00Z').getAttribute('datetime')).toBe('2026-10-02T10:00:00Z');
    expect(screen.queryByText('unprojected-private-message')).toBeNull();
    expect(screen.queryByText('unprojected-reason')).toBeNull();
    expect(within(evidence).queryByText('Healthy')).toBeNull();
  });

  it('does not project forbidden conditions or manifest internals into Summary', async () => {
    details.set(pod.id, { ...pod, capabilities: { conditions: { allowed: false }, manifest: { allowed: false } }, conditions: [{ type: 'HiddenCondition', status: 'True' }], manifest: { metadata: { ownerReferences: [{ uid: 'replica', controller: true }] }, data: { password: 'private-value' }, spec: { containers: [{ env: [{ value: 'private-env' }] }] } } });
    open(`?resource=${pod.id}&panel=conditions`);
    const panels = await screen.findByRole('navigation', { name: 'Resource panels' });
    expect(within(panels).getByRole('button', { name: 'Summary' }).getAttribute('aria-current')).toBe('page');
    expect(screen.queryByRole('table', { name: 'Reported conditions' })).toBeNull();
    for (const text of ['HiddenCondition', 'private-value', 'private-env', 'Controller']) expect(screen.queryByText(text)).toBeNull();
  });

  it('labels only an explicitly permitted controller reference and keeps other owners distinct', async () => {
    details.set(pod.id, { ...pod, owner_uids: ['replica', 'deployment'], capabilities: { manifest: { allowed: true } }, manifest: { metadata: { ownerReferences: [{ uid: 'replica', controller: true }, { uid: 'deployment', controller: false }] } } });
    open(`?resource=${pod.id}`);
    const related = await screen.findByRole('region', { name: 'Related resources' });
    expect(within(related).getByRole('link', { name: 'Inspect related ReplicaSet api-release in shop' }).textContent).toContain('Controller');
    expect(within(related).getByRole('link', { name: 'Inspect related Deployment api in shop' }).textContent).toContain('Owner');
    expect(within(related).getAllByText(/^Controller · /)).toHaveLength(1);
  });
});
