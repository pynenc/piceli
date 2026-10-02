import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import type { Component, Environment, Source } from './Composition';
import { compositionGraph, compositionNodeId } from './compositionGraph';
import { CompositionTopology } from './CompositionTopology';
import { compareComponentVersions, CompositionVersions } from './CompositionVersions';
import { compositionAttention, CompositionAttention } from './CompositionAttention';

const component: Component = { name: 'api', source: 'product', commit: 'a'.repeat(40), digest: `sha256:${'b'.repeat(64)}`, health: 'healthy', state: 'synced' };
const sources: Source[] = [{ name: 'product', url: 'https://github.com/example/product', refs: { main: 'a'.repeat(40) } }, { name: 'unused', refs: {} }];
const environments: Environment[] = [
  { name: 'production', namespace: 'shop', state: 'deployed', health: 'healthy', revision: { product: component.commit! }, components: [component, { ...component, name: 'worker' }, { name: 'cache', health: 'unknown', state: 'pending' }] },
  { name: 'staging', namespace: 'shop-stage', state: 'approval-required', health: 'unknown', revision: { product: 'c'.repeat(40) }, components: [{ ...component, commit: 'c'.repeat(40), digest: `sha256:${'d'.repeat(64)}` }] },
];
afterEach(cleanup);
function Location() { return <output aria-label="Comparison address">{useLocation().search}</output>; }

it('deduplicates shared source/environment nodes while keeping distinct component versions and only reported edges', () => {
  const graph = compositionGraph(sources, environments);
  expect(graph.nodes.filter(node => node.kind === 'source')).toHaveLength(2);
  expect(graph.nodes.filter(node => node.kind === 'environment')).toHaveLength(2);
  expect(graph.nodes.filter(node => node.kind === 'component')).toHaveLength(4);
  expect(new Set(graph.nodes.map(node => node.id)).size).toBe(graph.nodes.length);
  expect(graph.edges.filter(edge => edge.relation === 'source')).toHaveLength(3);
  expect(graph.edges.filter(edge => edge.relation === 'membership')).toHaveLength(4);
  expect(graph.edges.some(edge => edge.to === compositionNodeId('component', 'production', 'cache'))).toBe(false);
  expect(graph.edges.some(edge => edge.from === compositionNodeId('source', 'unused'))).toBe(false);
  expect(graph.edges.every(edge => edge.path.includes(' C'))).toBe(true);
  expect(graph.nodes.filter(node => node.name === 'api').map(node => node.component?.commit)).toEqual(['a'.repeat(40), 'c'.repeat(40)]);
});

it('keeps graph identities distinct even when names contain route or delimiter characters', () => {
  const graph = compositionGraph([], [
    { ...environments[0], name: 'a/b', components: [{ ...component, name: 'c' }] },
    { ...environments[0], name: 'a', components: [{ ...component, name: 'b/c' }] },
  ]);
  expect(new Set(graph.nodes.map(node => node.id)).size).toBe(graph.nodes.length);
  expect(graph.edges).toHaveLength(4);
});

it('supports native keyboard selection and zoom without changing reported topology', async () => {
  const onSelect = vi.fn();
  render(<CompositionTopology sources={sources} environments={environments} selected={{ kind: 'component', environment: 'production', name: 'api' }} onSelect={onSelect} />);
  const source = screen.getByRole('button', { name: 'Select source product' });
  source.focus();
  await userEvent.setup().keyboard('{ArrowRight}{Enter}');
  expect(onSelect).toHaveBeenCalledWith({ kind: 'component', environment: 'production', name: 'api' });
  expect(screen.getByLabelText('Topology zoom level').textContent).toBe('100%');
  await userEvent.setup().click(screen.getByRole('button', { name: 'Zoom in topology' }));
  expect(screen.getByLabelText('Topology zoom level').textContent).toBe('115%');
  await userEvent.setup().click(screen.getByRole('button', { name: 'Fit width' }));
  expect(screen.getByLabelText('Topology zoom level').textContent).toBe('100%');
  expect(screen.getAllByRole('button', { name: /^Inspect / })).toHaveLength(4);
});

it('lets a keyboard user zoom and reset the canvas without changing selected data', async () => {
  const onSelect = vi.fn();
  render(<CompositionTopology sources={sources} environments={environments} onSelect={onSelect} />);
  const canvas = screen.getByRole('region', { name: 'Infrastructure topology' });
  canvas.focus();
  await userEvent.setup().keyboard('+');
  expect(screen.getByLabelText('Topology zoom level').textContent).toBe('115%');
  await userEvent.setup().keyboard('0');
  expect(screen.getByLabelText('Topology zoom level').textContent).toBe('100%');
  expect(onSelect).not.toHaveBeenCalled();
});

it('treats a source polling timestamp as an observation without claiming a live connection', () => {
  render(<CompositionTopology sources={[{ ...sources[0], last_poll: '2020-01-01T00:00:00Z' }]} environments={environments} onSelect={vi.fn()} />);
  const source = screen.getByRole('button', { name: 'Select source product' });
  expect(within(source).getByText('Observed').classList.contains('neutral')).toBe(true);
  expect(within(source).queryByText('Connected')).toBeNull();
});

it('describes suspended environments faithfully without reporting progress or an incident', () => {
  const signals = compositionAttention([], [{ ...environments[0], state: 'suspended', health: 'suspended', components: [] }]);
  expect(signals).toHaveLength(1);
  expect(signals[0]).toMatchObject({ category: 'lifecycle', title: 'Environment suspended', detail: 'suspended' });
});

it('compares complete reported source/commit/digest identities and preserves unknown evidence', () => {
  expect(compareComponentVersions(component, { ...component }).state).toBe('same');
  expect(compareComponentVersions(component, { ...component, digest: 'sha256:different' }).state).toBe('different');
  expect(compareComponentVersions(component, { ...component, commit: null, digest: null }).state).toBe('unknown');
  expect(compareComponentVersions(component, undefined).state).toBe('unknown');
  expect(compareComponentVersions({ ...component, source: 'another-repo' }, component)).toMatchObject({ state: 'different', commit: 'unknown', digest: 'same' });
});

it('compares selected environments and links exact GitHub commits without ancestry claims', async () => {
  const inspect = vi.fn();
  render(<MemoryRouter><CompositionVersions environments={environments} sources={sources} onInspect={inspect} /></MemoryRouter>);
  const row = screen.getByRole('rowheader', { name: /api/ }).closest('tr')!;
  expect(within(row).getByText('Different')).toBeTruthy();
  expect(within(row).getByRole('link', { name: /Compare in GitHub/ }).getAttribute('href')).toBe(`https://github.com/example/product/compare/${'a'.repeat(40)}...${'c'.repeat(40)}`);
  await userEvent.setup().click(within(row).getByRole('button', { name: 'Inspect target →' }));
  expect(inspect).toHaveBeenCalledWith('staging', 'api');
  await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'Target environment' }), 'production');
  expect(within(row).getByText('Same')).toBeTruthy();
  expect(within(row).queryByRole('link', { name: /Compare in GitHub/ })).toBeNull();
  const cache = screen.getByRole('rowheader', { name: /cache/ }).closest('tr')!;
  expect(within(cache).getByText('Unknown')).toBeTruthy();
});

it('separates reported failures from approval gates and progressing or stopped states', () => {
  const attentionSources = [{ ...sources[0], error: 'Source fetch unavailable' }];
  const attentionEnvironments = [{ ...environments[0], state: 'stopped', components: [{ ...component, name: 'failed-api', state: 'failed' }, { ...component, name: 'building-worker', state: 'building', health: 'unknown' }] }, environments[1]];
  const signals = compositionAttention(attentionSources, attentionEnvironments);
  expect(signals.filter(signal => signal.category === 'investigate').map(signal => signal.scope)).toEqual(['product', 'production / failed-api']);
  expect(signals.filter(signal => signal.category === 'decision').map(signal => signal.scope)).toEqual(['staging']);
  expect(signals.filter(signal => signal.category === 'lifecycle').map(signal => signal.scope)).toEqual(['production', 'production / building-worker']);
  render(<MemoryRouter><CompositionAttention sources={attentionSources} environments={attentionEnvironments} /></MemoryRouter>);
  expect(within(screen.getByRole('region', { name: 'Needs investigation' })).queryByText('production / building-worker')).toBeNull();
  expect(within(screen.getByRole('region', { name: 'Decisions waiting' })).getByRole('link', { name: 'Inspect' }).getAttribute('href')).toBe('/composition/environments/staging');
});

it('restores comparison choices from the URL and preserves missing selection until the user replaces it', async () => {
  render(<MemoryRouter initialEntries={['/composition/overview?view=versions&baseline=removed&compare=production']}><CompositionVersions environments={environments} sources={sources} onInspect={vi.fn()} /><Location /></MemoryRouter>);
  expect(screen.getByText('Comparison selection unavailable')).toBeTruthy();
  expect(screen.queryByRole('table')).toBeNull();
  expect((screen.getByRole('combobox', { name: 'Target environment' }) as HTMLSelectElement).value).toBe('production');
  await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'Baseline environment' }), 'staging');
  expect(screen.getByRole('table')).toBeTruthy();
  expect(screen.getByLabelText('Comparison address').textContent).toBe('?view=versions&baseline=staging&compare=production');
  const row = screen.getByRole('rowheader', { name: /api/ }).closest('tr')!;
  expect(within(row).getByRole('link', { name: /Compare in GitHub/ }).getAttribute('href')).toBe(`https://github.com/example/product/compare/${'c'.repeat(40)}...${'a'.repeat(40)}`);
});
