import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { api } from '../../api/client';
import { formatTime } from '../../components/State';
import { CompositionSources, type Environment, type Source } from './Composition';

const revision = 'a'.repeat(40);
const assetsRevision = 'c'.repeat(40);
const sources: Source[] = [
  { name: 'product', url: 'https://github.com/example/product.git', refs: { main: revision, 'release/v1': 'b'.repeat(40) }, last_poll: '2026-10-02T12:00:00Z' },
  { name: 'assets', url: 'https://git.example/assets.git', refs: { main: assetsRevision }, error: 'Source fetch unavailable' },
  { name: 'unreferenced', refs: { main: revision } },
];
const environments: Environment[] = [
  { name: 'production', namespace: 'shop', state: 'deployed', health: 'healthy', revision: { product: revision }, components: [{ name: 'api', source: 'product', state: 'synced', health: 'healthy' }, { name: 'cache', source: 'assets', state: 'synced', health: 'healthy' }] },
  { name: 'preview/blue', namespace: null, state: 'pending', health: 'unknown', revision: { assets: assetsRevision }, components: [] },
];
let snapshot: { configured: boolean; controller: null; sources: Source[]; environments: Environment[] };

beforeEach(() => {
  snapshot = { configured: true, controller: null, sources: structuredClone(sources), environments: structuredClone(environments) };
  vi.spyOn(api, 'composition').mockImplementation(async () => snapshot);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function Location() { return <output aria-label="Current source address">{useLocation().search}</output>; }
function open(search = '') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[`/composition/sources${search}`]}><CompositionSources /><Location /></MemoryRouter></QueryClientProvider>);
}

describe('compact source inventory', () => {
  it('keeps every ref readable and reveals full identities and safe commit links on disclosure', async () => {
    open();
    const source = await screen.findByRole('region', { name: 'Source product' });
    expect(within(source).getByText('release/v1')).toBeTruthy();
    const detail = within(source).getByLabelText('Revision for product ref main') as HTMLDetailsElement;
    expect(detail.open).toBe(false);
    const summary = detail.querySelector('summary')!;
    await userEvent.setup().click(summary);
    expect(detail.open).toBe(true);
    expect(within(detail).getByText(revision)).toBeTruthy();
    expect(within(detail).getByRole('link', { name: 'Open commit' }).getAttribute('href')).toBe(`https://github.com/example/product/commit/${revision}`);
    expect(within(source).getByRole('link', { name: 'Open repository' }).getAttribute('href')).toBe('https://github.com/example/product');
    expect(within(source).getByText(formatTime(sources[0].last_poll))).toBeTruthy();
  });

  it('searches reported component associations while preserving unrelated URL state and source details', async () => {
    open('?keep=selection');
    const search = await screen.findByRole('searchbox', { name: 'Filter sources' });
    await userEvent.setup().type(search, 'cache');
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Source product' })).toBeNull());
    const source = screen.getByRole('region', { name: 'Source assets' });
    expect(within(source).getByText('https://git.example/assets.git')).toBeTruthy();
    expect(within(source).getByText('Source fetch unavailable')).toBeTruthy();
    expect(within(source).getByText('main')).toBeTruthy();
    expect(screen.getByLabelText('Current source address').textContent).toBe('?keep=selection&sourceSearch=cache');
    expect(screen.getByText('1 of 3 sources')).toBeTruthy();
    expect(vi.mocked(api.composition)).toHaveBeenCalledTimes(1);
  });

  it('restores a full-revision search from the URL and clears to the complete inventory', async () => {
    open(`?sourceSearch=${assetsRevision}`);
    const search = await screen.findByRole('searchbox', { name: 'Filter sources' });
    expect((search as HTMLInputElement).value).toBe(assetsRevision);
    expect(screen.queryByRole('region', { name: 'Source product' })).toBeNull();
    expect(screen.getByRole('region', { name: 'Source assets' })).toBeTruthy();
    await userEvent.setup().clear(search);
    expect(screen.getByRole('region', { name: 'Source product' })).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Source unreferenced' })).toBeTruthy();
    expect(screen.getByLabelText('Current source address').textContent).toBe('');
  });

  it('provides exact component and source topology destinations without associating matching commits', async () => {
    open();
    const product = await screen.findByRole('region', { name: 'Destinations for product' });
    const component = within(product).getByRole('link', { name: 'Inspect api in production' });
    expect(component.getAttribute('href')).toBe('/composition/overview?environment=production&component=api');
    expect(within(product).queryByText('cache')).toBeNull();
    const assets = screen.getByRole('region', { name: 'Destinations for assets' });
    expect(within(assets).getByRole('link', { name: 'Environment preview/blue' }).getAttribute('href')).toBe('/composition/environments/preview%2Fblue');
    const topology = within(assets).getByRole('link', { name: 'View assets topology in preview/blue' });
    expect(new URL(topology.getAttribute('href')!, 'http://localhost').searchParams.get('environment')).toBe('preview/blue');
    expect(within(assets).getByText('No component source reported')).toBeTruthy();
    expect(within(assets).getByText('Namespace pending')).toBeTruthy();
    const unreferenced = screen.getByRole('region', { name: 'Destinations for unreferenced' });
    expect(within(unreferenced).queryByRole('link')).toBeNull();
  });

  it('explains an empty match without hiding the clear action', async () => {
    open('?sourceSearch=missing');
    await screen.findByRole('heading', { name: 'No matching sources' });
    await userEvent.setup().click(screen.getByRole('button', { name: 'Clear source filter' }));
    expect(screen.getByRole('region', { name: 'Source product' })).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Source assets' })).toBeTruthy();
  });

  it('distinguishes an observed empty inventory from missing controller configuration', async () => {
    snapshot.sources = [];
    open();
    await screen.findByRole('heading', { name: 'No sources reported' });
    expect(screen.queryByRole('region', { name: /^Source / })).toBeNull();
    cleanup();
    snapshot.configured = false;
    open();
    await screen.findByText('No GitOps controller status yet');
    expect(screen.queryByRole('heading', { name: 'No sources reported' })).toBeNull();
  });
});
