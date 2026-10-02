import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Resource } from '../api/generated';
import { orderByOwner } from '../features/observation/topology';
import { ResourceTopology } from './ResourceTopology';
import { layoutResourceTopology, relatedResourceIds } from './ownerGraphLayout';
import { ResourceConfiguration } from './Resources';

const resource = (kind: string, name: string, uid: string, owner_uids: string[] = []): Resource => ({
  id: uid, identity: { target_id: 'test-cluster', api_version: 'v1', namespace: 'shop', kind, name, uid },
  owner_uids, presence: 'present', ownership: 'managed', health: 'unknown', relation: 'unknown',
});
const deployment = resource('Deployment', 'api', 'deployment');
const replica = resource('ReplicaSet', 'api-release', 'replica', ['deployment']);
const pod = resource('Pod', 'api-pod', 'pod', ['replica']);

afterEach(cleanup);

describe('observed resource topology', () => {
  it('draws only explicit owner references, retaining independent and partially observed resources', () => {
    const service = resource('Service', 'api', 'service');
    const orphan = resource('Pod', 'other', 'other', ['outside-inventory']);
    const graph = layoutResourceTopology(orderByOwner([pod, service, replica, orphan, deployment]));
    expect(graph.edges.map(edge => [edge.ownerId, edge.resourceId])).toEqual([['deployment', 'replica'], ['replica', 'pod']]);
    expect(graph.nodes.map(node => node.resource.id).sort()).toEqual(['deployment', 'other', 'pod', 'replica', 'service']);
    expect(graph.nodes.find(node => node.resource.id === 'other')?.outsideOwners).toBe(1);
    expect(graph.nodes.find(node => node.resource.id === 'replica')!.x).toBeGreaterThan(graph.nodes.find(node => node.resource.id === 'deployment')!.x);
    expect(graph.nodes.find(node => node.resource.id === 'pod')!.x).toBeGreaterThan(graph.nodes.find(node => node.resource.id === 'replica')!.x);
  });

  it('keeps a filtered owner outside the view instead of inventing a replacement', () => {
    const graph = layoutResourceTopology(orderByOwner([pod, deployment]));
    expect(graph.edges).toEqual([]);
    expect(graph.nodes.find(node => node.resource.id === 'pod')).toMatchObject({ depth: 0, owners: [], outsideOwners: 1 });
  });

  it('handles cycles, self references and multiple owners without dropping identities', () => {
    const resources = [resource('Pod', 'one', 'one', ['two', 'one']), resource('Pod', 'two', 'two', ['one', 'outside', 'one'])];
    const graph = layoutResourceTopology(orderByOwner(resources));
    expect(graph.nodes).toHaveLength(2);
    expect(graph.edges.map(edge => [edge.ownerId, edge.resourceId])).toEqual([['two', 'one'], ['one', 'one'], ['one', 'two']]);
    expect(graph.nodes.every(node => Number.isFinite(node.x) && Number.isFinite(node.y))).toBe(true);
    expect(graph.edges.every(edge => !edge.path.includes('NaN'))).toBe(true);
  });

  it('groups independent kinds without introducing network or configuration connections', () => {
    const service = resource('Service', 'api', 'service');
    const config = resource('ConfigMap', 'api', 'config');
    const graph = layoutResourceTopology(orderByOwner([deployment, replica, pod, service, config]));
    expect(graph.groups.find(group => group.label === 'Deployment / api')?.resourceIds).toEqual(['deployment', 'replica', 'pod']);
    expect(graph.groups.find(group => group.label === 'Networking')?.resourceIds).toEqual(['service']);
    expect(graph.groups.find(group => group.label === 'Configuration')?.resourceIds).toEqual(['config']);
    expect([...relatedResourceIds(graph.edges, 'pod')].sort()).toEqual(['pod', 'replica']);
    expect([...relatedResourceIds(graph.edges, 'service')]).toEqual(['service']);
  });

  it('focuses direct relationships without opening an inspector or hiding inventory', async () => {
    const inspect = vi.fn();
    render(<ResourceTopology rows={orderByOwner([pod, replica, deployment])} onSelect={inspect} />);
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole('combobox', { name: 'Focus resource' }), 'replica');
    expect(screen.getByRole('status', { name: 'Graph focus' }).textContent).toContain('1 observed owner · 1 direct child');
    expect(screen.getAllByRole('button', { name: /^Inspect / })).toHaveLength(3);
    expect(inspect).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Clear focus' }));
    expect((screen.getByRole('combobox', { name: 'Focus resource' }) as HTMLSelectElement).value).toBe('');
    expect(screen.getByRole('status', { name: 'Graph focus' }).textContent).toContain('Kubernetes owner reference');
  });

  it('fits and resets the diagram while every resource remains inspectable', async () => {
    const inspect = vi.fn();
    render(<ResourceTopology rows={orderByOwner([pod, replica, deployment])} onSelect={inspect} />);
    const viewport = screen.getByRole('region', { name: 'Resource list' });
    Object.defineProperty(viewport, 'clientWidth', { value: 600 });
    Object.defineProperty(viewport, 'clientHeight', { value: 400 });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Fit graph' }));
    expect(Number.parseInt(screen.getByLabelText('Graph scale').textContent!)).toBeLessThan(100);
    await user.click(screen.getByRole('button', { name: 'Zoom in' }));
    await user.click(screen.getByRole('button', { name: 'Reset zoom' }));
    expect(screen.getByLabelText('Graph scale').textContent).toBe('100%');
    await user.click(screen.getByRole('button', { name: 'Inspect Pod api-pod in shop' }));
    expect(inspect).toHaveBeenCalledWith(pod);
  });

  it('shows reported image and port evidence on graph nodes without reading configuration values', () => {
    const workload = { ...pod, images: ['registry.example/api:v2'], containers: ['api', 'metrics'], ports: [8080, 9090] };
    const service = { ...resource('Service', 'api', 'service'), ports: [80] };
    const secret = { ...resource('Secret', 'settings', 'secret'), manifest: { data: { private: 'private-value' } } };
    render(<ResourceTopology rows={orderByOwner([workload, service, secret])} onSelect={vi.fn()} />);
    const node = screen.getByRole('button', { name: 'Inspect Pod api-pod in shop' });
    expect(within(node).getByText('registry.example/api:v2')).toBeTruthy();
    expect(within(node).getByText('2 containers · Ports 8080, 9090')).toBeTruthy();
    expect(within(screen.getByRole('button', { name: 'Inspect Service api in shop' })).getByText('Port 80')).toBeTruthy();
    expect(screen.queryByText('private-value')).toBeNull();
  });

  it('opens the focused resource from its context controls', async () => {
    const inspect = vi.fn();
    render(<ResourceTopology rows={orderByOwner([pod, replica, deployment])} onSelect={inspect} />);
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole('combobox', { name: 'Focus resource' }), 'replica');
    await user.click(screen.getByRole('button', { name: 'Open details for ReplicaSet api-release' }));
    expect(inspect).toHaveBeenCalledWith(replica);
  });

  it('shows only supplied configuration and makes missing observations explicit', () => {
    const { rerender } = render(<ResourceConfiguration resource={{ ...pod, containers: ['api', 'metrics'], ports: [8080, 9090], images: ['example.invalid/api:v1'] }} />);
    expect(within(screen.getByRole('list', { name: 'Reported ports' })).getAllByRole('listitem').map(item => item.textContent)).toEqual(['8080', '9090']);
    expect(within(screen.getByRole('list', { name: 'Reported containers' })).getAllByRole('listitem').map(item => item.textContent)).toEqual(['api', 'metrics']);
    expect(screen.getByText('example.invalid/api:v1')).toBeTruthy();
    rerender(<ResourceConfiguration resource={pod} />);
    expect(screen.queryByRole('list', { name: 'Reported ports' })).toBeNull();
    expect(screen.getByText('No ports reported.')).toBeTruthy();
    expect(screen.getByText('No image information reported.')).toBeTruthy();
    expect(screen.getByText('No container names reported.')).toBeTruthy();
  });

  it('supports owner and child keyboard navigation and inspects the exact resource', async () => {
    const inspect = vi.fn();
    render(<ResourceTopology rows={orderByOwner([pod, replica, deployment])} onSelect={inspect} />);
    const user = userEvent.setup();
    const deploymentButton = screen.getByRole('button', { name: 'Inspect Deployment api in shop' });
    const replicaButton = screen.getByRole('button', { name: 'Inspect ReplicaSet api-release in shop' });
    const podButton = screen.getByRole('button', { name: 'Inspect Pod api-pod in shop' });
    deploymentButton.focus();
    await user.keyboard('{ArrowRight}');
    expect(document.activeElement).toBe(replicaButton);
    await user.keyboard('{ArrowRight}{Enter}');
    expect(document.activeElement).toBe(podButton);
    expect(inspect).toHaveBeenCalledWith(pod);
    await user.keyboard('{ArrowLeft}');
    expect(document.activeElement).toBe(replicaButton);
    expect(screen.getByText('Owner: ReplicaSet api-release')).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Resource list' })).toBeTruthy();
  });
});
