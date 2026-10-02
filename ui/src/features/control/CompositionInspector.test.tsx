import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ComponentProps } from 'react';
import type { Component, Environment, Source } from './Composition';
import { CompositionInspector } from './CompositionInspector';

const sha = 'a'.repeat(40);
const nextSha = 'b'.repeat(40);
const digest = `sha256:${'c'.repeat(64)}`;
const component: Component = { name: 'api', source: 'product', commit: sha, digest, state: 'synced', health: 'healthy' };
const source: Source = { name: 'product', url: 'https://github.com/example/product', refs: { main: sha, release: sha, short: sha.slice(0, 7), other: nextSha } };
const environment: Environment = { name: 'production', namespace: 'shop', state: 'deployed', health: 'healthy', revision: { product: sha }, components: [component] };
const environments: Environment[] = [environment,
  { ...environment, name: 'staging', components: [{ ...component, commit: nextSha, digest: `sha256:${'d'.repeat(64)}` }] },
  { ...environment, name: 'preview', state: 'building', components: [{ ...component, state: 'building' }] },
  { ...environment, name: 'unresolved', components: [{ ...component, commit: null, digest: null }] },
  { ...environment, name: 'unrelated', components: [{ ...component, source: 'another-source' }] },
];

function open(props: Partial<ComponentProps<typeof CompositionInspector>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const onInspect = vi.fn();
  const onInspectSource = vi.fn();
  const supplied = { selection: { kind: 'component' as const, name: component.name, environment: environment.name }, environment, component, source, requestedComponent: component.name, onClear: vi.fn(), environments, sources: [source], onInspect, onInspectSource, ...props };
  const view = render(<QueryClientProvider client={client}><MemoryRouter><CompositionInspector {...supplied} /></MemoryRouter></QueryClientProvider>);
  return { ...view, onInspect, onInspectSource };
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('preserves exact selected component evidence and existing source/environment links', () => {
  open();
  const inspector = within(screen.getByRole('complementary', { name: 'Selected component' }));
  expect(inspector.getByRole('heading', { name: 'api' })).toBeTruthy();
  const evidence = within(inspector.getByRole('region', { name: 'Selected version evidence' }));
  expect(evidence.getByText(sha)).toBeTruthy();
  expect(evidence.getByText(digest)).toBeTruthy();
  expect(evidence.getByText('Commit')).toBeTruthy();
  expect(evidence.getByText('Image digest')).toBeTruthy();
  expect(inspector.getByRole('link', { name: /View commit in GitHub/ }).getAttribute('href')).toBe(`https://github.com/example/product/commit/${sha}`);
  expect(inspector.getByRole('link', { name: 'Open production' }).getAttribute('href')).toBe('/composition/environments/production');
});

it('matches only full exact source commits to published refs and opens the known source', async () => {
  const { onInspectSource } = open();
  const refs = within(screen.getByRole('list', { name: 'Published refs at this commit' }));
  expect(refs.getByText('main')).toBeTruthy();
  expect(refs.getByText('release')).toBeTruthy();
  expect(refs.queryByText('short')).toBeNull();
  expect(refs.queryByText('other')).toBeNull();
  await userEvent.setup().click(screen.getByRole('button', { name: 'Inspect source product' }));
  expect(onInspectSource).toHaveBeenCalledWith('product');
});

it('connects only same-source counterparts and compares complete reported identities', async () => {
  const { onInspect } = open();
  const staging = within(screen.getByRole('article', { name: 'api in staging' }));
  expect(staging.getByText('Different commit')).toBeTruthy();
  expect(staging.getByText('Different image')).toBeTruthy();
  const preview = within(screen.getByRole('article', { name: 'api in preview' }));
  expect(preview.getByText('Same commit')).toBeTruthy();
  expect(preview.getByText('Same image')).toBeTruthy();
  const unresolved = within(screen.getByRole('article', { name: 'api in unresolved' }));
  expect(unresolved.getByText('Commit unreported')).toBeTruthy();
  expect(unresolved.getByText('Image unreported')).toBeTruthy();
  expect(screen.queryByRole('article', { name: 'api in unrelated' })).toBeNull();
  await userEvent.setup().click(staging.getByRole('button', { name: 'Open api counterpart in staging' }));
  expect(onInspect).toHaveBeenCalledWith('staging', 'api');
  const compare = new URL(staging.getByRole('link', { name: 'Compare versions' }).getAttribute('href')!, 'http://localhost');
  expect(compare.searchParams.get('baseline')).toBe('production');
  expect(compare.searchParams.get('compare')).toBe('staging');
  expect(compare.searchParams.get('focus')).toBe('api');
});

it('links only the selected application views allowed by its reported capabilities', async () => {
  const requests: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    requests.push(new URL(request.url).pathname);
    return Response.json({ id: 'shop/main', name: 'Shop', target: { id: 'target', name: 'cluster', namespace: 'shop' }, capabilities: { inspect: { allowed: true }, activity: { allowed: true }, evaluate: { allowed: false } } });
  }));
  open({ environment: { ...environment, application_id: 'shop/main' }, environments: environments.map(item => ({ ...item, application_id: `app-${item.name}` })) });
  const resources = await screen.findByRole('link', { name: 'Inspect workloads and logs →' });
  expect(resources.getAttribute('href')).toBe('/applications/shop%2Fmain/resources');
  expect(screen.getByRole('link', { name: 'Open deployment activity →' }).getAttribute('href')).toBe('/applications/shop%2Fmain/activity');
  expect(screen.queryByRole('link', { name: 'Review application changes →' })).toBeNull();
  expect(requests).toEqual(['/api/v1/applications/shop%2Fmain']);
});

it('does not expose another application’s capabilities as selected environment links', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ id: 'wrong-app', target: { id: 'target', name: 'cluster', namespace: 'shop' }, capabilities: { inspect: { allowed: true }, evaluate: { allowed: true } } })));
  open({ environment: { ...environment, application_id: 'shop/main' } });
  await waitFor(() => expect(screen.queryByText('Checking available application views…')).toBeNull());
  expect(screen.queryByRole('link', { name: 'Inspect workloads and logs →' })).toBeNull();
  expect(screen.queryByRole('link', { name: 'Review application changes →' })).toBeNull();
});

it('connects known source destinations without fetching all their applications or matching by commit alone', async () => {
  const fetch = vi.fn();
  vi.stubGlobal('fetch', fetch);
  const { onInspect } = open({ selection: { kind: 'source', name: source.name }, component: undefined, requestedComponent: null, environments: environments.map(item => ({ ...item, application_id: `app-${item.name}`, revision: {} })) });
  expect(screen.getByRole('link', { name: 'View main commit in GitHub' }).getAttribute('href')).toBe(`https://github.com/example/product/commit/${sha}`);
  expect(screen.queryByRole('link', { name: 'View short commit in GitHub' })).toBeNull();
  await userEvent.setup().click(screen.getByRole('button', { name: 'Open api from product in staging' }));
  expect(onInspect).toHaveBeenCalledWith('staging', 'api');
  expect(screen.queryByRole('button', { name: 'Open api from product in unrelated' })).toBeNull();
  expect(fetch).not.toHaveBeenCalled();
});

it('keeps incomplete selected commit evidence unknown and does not derive external links from a mismatched source', () => {
  open({ component: { ...component, commit: sha.slice(0, 7) }, source: { ...source, name: 'unrelated' }, sources: [] });
  expect(screen.getByText('Source details are not reported. Published refs cannot be matched.')).toBeTruthy();
  expect(screen.queryByRole('link', { name: /View commit in GitHub/ })).toBeNull();
  const preview = within(screen.getByRole('article', { name: 'api in preview' }));
  expect(preview.getByText('Full commit needed')).toBeTruthy();
  expect(preview.queryByText('Same commit')).toBeNull();
});

it('shows the controller’s last action and a failed checks verification without rolling', () => {
  const verified: Environment = { ...environment, health: 'degraded', last_action: 'verified', verification: { state: 'failed', trigger: 'checks-changed', checks_hash: 'e'.repeat(64), at: '2026-10-01T09:02:00Z', failed: [{ check: 'http-ready', code: 'check-failed' }] } };
  open({ selection: { kind: 'environment', name: verified.name }, environment: verified, component: undefined, requestedComponent: null });
  const inspector = within(screen.getByRole('complementary', { name: 'Selected environment' }));
  expect(inspector.getByText('Last action')).toBeTruthy();
  expect(inspector.getByText('Verified')).toBeTruthy();
  const notice = inspector.getByText('Checks verification failed').closest('.notice')!;
  expect(notice.textContent).toContain('http-ready');
  expect(notice.textContent).toContain('check-failed');
  expect(notice.textContent).toContain('nothing was rolled back');
});

it('does not invent verification evidence an older controller never reported', () => {
  open({ selection: { kind: 'environment', name: environment.name }, component: undefined, requestedComponent: null });
  const inspector = within(screen.getByRole('complementary', { name: 'Selected environment' }));
  expect(inspector.getByText('Last action')).toBeTruthy();
  expect(inspector.getByText('Not reported')).toBeTruthy();
  expect(inspector.queryByText(/verification/i)).toBeNull();
});
