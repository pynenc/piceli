import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { CompositionOverview, CompositionSources } from './Composition';
import { compositionHome, compositionTitle } from './compositionRoutes';
import type { Capabilities } from '../../api/generated';

const overview = {
  configured: true, controller: { state: 'running', last_poll: '2026-10-02T09:00:00Z' },
  sources: [
    { name: 'product', url: 'https://git.example/product', refs: { main: 'a'.repeat(40) }, last_poll: '2026-10-02T09:00:00Z' },
    { name: 'unreferenced', refs: { main: 'a'.repeat(40) } },
  ],
  environments: [
    { name: 'production', namespace: 'shop', state: 'deployed', health: 'healthy', revision: { product: 'a'.repeat(40) }, application_id: 'shop/main', components: [
      { name: 'api', source: 'product', commit: 'a'.repeat(40), digest: `sha256:${'b'.repeat(64)}`, state: 'synced', health: 'healthy', updated_at: '2026-10-02T09:00:00Z' },
      { name: 'cache', source: null, commit: null, digest: null, state: 'pending', health: 'unknown', reason: 'No observation published yet' },
    ] },
    { name: 'preview', namespace: null, state: 'approval-required', health: 'unknown', revision: {}, plan_hash: `sha256:${'c'.repeat(64)}`, components: [] },
  ],
};

function Location() { return <output aria-label="Current location">{useLocation().search}</output>; }

function open({ path = '/composition/overview', source = false, configured = true, snapshot = overview, canSync = false }: { path?: string; source?: boolean; configured?: boolean; snapshot?: typeof overview; canSync?: boolean } = {}) {
  const requests: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    requests.push(`${request.method} ${path}`);
    if (path.startsWith('/api/v1/applications/')) return Response.json({ id: 'shop/main', name: 'Shop main', definition_kind: 'inventory', ownership: 'inventory', target: { id: 'fake', name: 'Fake', namespace: 'shop' }, capabilities: { inspect: { allowed: true }, activity: { allowed: false } }, freshness: { state: 'connected' } });
    return Response.json(configured ? snapshot : { configured: false, controller: null, sources: [], environments: [] });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}>{source ? <CompositionSources /> : <CompositionOverview canSync={canSync} />}<Location /></MemoryRouter></QueryClientProvider>);
  return requests;
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('switches the environment canvas from the rail while retaining comparison choices and reported revisions', async () => {
  const requests = open({ path: '/composition/overview?environment=production&component=api&scope=all&baseline=production&compare=preview' });
  const rail = await screen.findByRole('group', { name: 'Environment switchboard' });
  const production = within(rail).getByRole('button', { name: 'Show environment production' });
  expect(production.getAttribute('aria-pressed')).toBe('false');
  expect(within(production).getByText('aaaaaaa').getAttribute('title')).toBe('a'.repeat(40));
  expect(within(production).getByText('Deployed')).toBeTruthy();
  const preview = within(rail).getByRole('button', { name: 'Show environment preview' });
  expect(within(preview).getByText('Revision unreported')).toBeTruthy();
  preview.focus();
  await userEvent.setup().keyboard('{Enter}');
  expect(preview.getAttribute('aria-pressed')).toBe('true');
  expect(screen.queryByRole('button', { name: 'Inspect api in production' })).toBeNull();
  expect((screen.getByRole('checkbox', { name: 'All environments' }) as HTMLInputElement).checked).toBe(false);
  expect(screen.getByLabelText('Current location').textContent).toBe('?environment=preview&baseline=production&compare=preview');
  expect(requests.every(request => request.startsWith('GET '))).toBe(true);
});

it('keeps duplicate inventory collapsed with its existing environment actions available on disclosure', async () => {
  open({ canSync: true });
  const disclosure = await screen.findByText('Environment inventory', { selector: 'summary span' });
  const details = disclosure.closest('details')!;
  expect(details.open).toBe(false);
  expect(within(details).getByRole('region', { name: 'Environment preview' }).closest('details')).toBe(details);
  await userEvent.setup().click(disclosure);
  expect(details.open).toBe(true);
  const inventory = screen.getByRole('region', { name: 'Environment preview' });
  expect(within(inventory).getByRole('button', { name: 'Sync preview' })).toBeTruthy();
  expect(within(inventory).getByRole('link', { name: 'Open environment' }).getAttribute('href')).toBe('/composition/environments/preview');
});

it('expands the same topology and inspector with Escape, focus return and selection preserved', async () => {
  open();
  const expand = await screen.findByRole('button', { name: 'Expand topology' });
  await userEvent.setup().click(expand);
  const dialog = screen.getByRole('dialog', { name: 'Infrastructure explorer' });
  expect(screen.getAllByRole('region', { name: 'Component dependencies' })).toHaveLength(1);
  await userEvent.setup().click(within(dialog).getByRole('button', { name: 'Inspect cache in production' }));
  expect(within(dialog).getByRole('complementary', { name: 'Selected component' }).textContent).toContain('cache');
  await userEvent.setup().keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(document.activeElement).toBe(expand);
  expect(screen.getByRole('button', { name: 'Inspect cache in production' }).getAttribute('aria-pressed')).toBe('true');
  expect(screen.getByLabelText('Current location').textContent).toBe('?environment=production&component=cache');
});

it('labels a source poll as observed without asserting a current connection', async () => {
  open({ source: true });
  const source = await screen.findByRole('region', { name: 'Source product' });
  expect(within(source).getByText('Observed').classList.contains('neutral')).toBe(true);
  expect(within(source).queryByText('Connected')).toBeNull();
});

it('shows reported relationships and exact component evidence without starting an action', async () => {
  const requests = open();
  const graph = await screen.findByRole('region', { name: 'Component dependencies' });
  expect(within(graph).getByRole('button', { name: 'Inspect api in production' }).getAttribute('aria-pressed')).toBe('true');
  const inspector = screen.getByRole('complementary', { name: 'Selected component' });
  expect(within(inspector).getByText(`sha256:${'b'.repeat(64)}`)).toBeTruthy();
  expect(within(inspector).getByText('a'.repeat(40))).toBeTruthy();
  expect((await within(inspector).findByRole('link', { name: /Inspect workloads/ })).getAttribute('href')).toBe('/applications/shop%2Fmain/resources');
  expect(screen.getByRole('button', { name: 'Show environment preview' })).toBeTruthy();
  expect(requests.every(request => ['GET /api/v1/composition', 'GET /api/v1/applications/shop%2Fmain'].includes(request))).toBe(true);
});

it('selects components by keyboard and retains unknown source and health as unknown', async () => {
  open();
  const control = await screen.findByRole('button', { name: 'Inspect cache in production' });
  control.focus();
  await userEvent.setup().keyboard('{Enter}');
  expect(control.getAttribute('aria-pressed')).toBe('true');
  const inspector = screen.getByRole('complementary', { name: 'Selected component' });
  expect(within(inspector).getByRole('heading', { name: 'cache' })).toBeTruthy();
  expect(within(inspector).getByText('Unknown')).toBeTruthy();
  expect(within(inspector).getByText('Not reported')).toBeTruthy();
  expect(within(inspector).getByText('Image unreported')).toBeTruthy();
  expect(within(inspector).getByText('No observation published yet')).toBeTruthy();
  expect(screen.getByLabelText('Current location').textContent).toContain('component=cache');
});

it('restores a selected component from its URL and switches to an empty environment honestly', async () => {
  open({ path: '/composition/overview?environment=production&component=cache' });
  const inspector = await screen.findByRole('complementary', { name: 'Selected component' });
  expect(within(inspector).getByRole('heading', { name: 'cache' })).toBeTruthy();
  await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'Environment' }), 'preview');
  expect(screen.getByRole('heading', { name: 'No components reported' })).toBeTruthy();
  expect(within(inspector).getByRole('heading', { name: 'No component selected' })).toBeTruthy();
  expect(screen.getByLabelText('Current location').textContent).toBe('?environment=preview');
  expect(screen.queryByRole('button', { name: /^Inspect / })).toBeNull();
});

it('associates sources by reported identity and never just by matching commit', async () => {
  open({ source: true });
  const destination = await screen.findByRole('region', { name: 'Destinations for product' });
  expect(within(destination).getByRole('link', { name: 'Environment production' }).getAttribute('href')).toBe('/composition/environments/production');
  expect(within(destination).getByText('api')).toBeTruthy();
  expect(within(destination).queryByText('cache')).toBeNull();
  const unrelated = screen.getByRole('region', { name: 'Destinations for unreferenced' });
  expect(within(unrelated).queryByRole('link')).toBeNull();
  expect(within(unrelated).getByText(/No environment references this source/)).toBeTruthy();
});

it('explains missing controller status without presenting reported summary or schematic', async () => {
  open({ configured: false });
  expect(await screen.findByText('No GitOps controller status yet')).toBeTruthy();
  expect(screen.queryByLabelText('Composition summary')).toBeNull();
  expect(screen.queryByRole('region', { name: 'Component dependencies' })).toBeNull();
});

it('opens the infrastructure overview while preserving the environment inventory route', () => {
  const capabilities = { actions: { composition: { allowed: true } } } as unknown as Capabilities;
  expect(compositionHome(capabilities)).toBe('/composition/overview');
  expect(compositionTitle('/composition/overview')).toBe('Overview');
  expect(compositionTitle('/composition')).toBe('Environments');
  expect(compositionTitle('/composition/sources')).toBe('Sources');
});

it('preserves an unavailable environment URL until explicit recovery without substituting another environment', async () => {
  open({ path: '/composition/overview?environment=removed&component=api' });
  expect(await screen.findByText('Selected environment unavailable')).toBeTruthy();
  expect(screen.queryByRole('region', { name: 'Component dependencies' })).toBeNull();
  expect(screen.queryByRole('complementary', { name: 'Selected component' })).toBeNull();
  expect(screen.getByLabelText('Current location').textContent).toBe('?environment=removed&component=api');
  await userEvent.setup().click(screen.getByRole('button', { name: 'Reset environment selection' }));
  expect(screen.getByRole('button', { name: 'Inspect api in production' }).getAttribute('aria-pressed')).toBe('true');
  expect(screen.getByLabelText('Current location').textContent).toBe('');
});

it('does not silently select a different component when the requested component disappears on refresh', async () => {
  const snapshot = structuredClone(overview);
  open({ path: '/composition/overview?environment=production&component=api', snapshot });
  const inspector = await screen.findByRole('complementary', { name: 'Selected component' });
  expect(within(inspector).getByRole('heading', { name: 'api' })).toBeTruthy();
  snapshot.environments[0].components.splice(0, 1);
  await userEvent.setup().click(screen.getByRole('button', { name: 'Refresh overview' }));
  expect(await within(inspector).findByText('Selected component unavailable')).toBeTruthy();
  expect(within(inspector).queryByRole('heading', { name: 'cache' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Inspect cache in production' }).getAttribute('aria-pressed')).toBe('false');
  expect(screen.getByLabelText('Current location').textContent).toBe('?environment=production&component=api');
  await userEvent.setup().click(screen.getByRole('button', { name: 'Clear component selection' }));
  expect(within(inspector).getByRole('heading', { name: 'cache' })).toBeTruthy();
  expect(screen.getByLabelText('Current location').textContent).toBe('?environment=production');
});

it('does not associate an unreferenced source whose name is inherited by JavaScript objects', async () => {
  const snapshot = structuredClone(overview);
  snapshot.sources.push({ name: 'constructor', refs: { main: 'f'.repeat(40) } });
  open({ source: true, snapshot });
  const destinations = await screen.findByRole('region', { name: 'Destinations for constructor' });
  expect(within(destinations).queryByRole('link')).toBeNull();
  expect(within(destinations).getByText(/No environment references this source/)).toBeTruthy();
});

it('opens the connected source inspector and includes all environments without duplicating source nodes', async () => {
  open();
  await userEvent.setup().click(await screen.findByRole('checkbox', { name: 'All environments' }));
  expect(screen.getByRole('button', { name: 'Select environment production' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Select environment preview' })).toBeTruthy();
  expect(screen.getAllByRole('button', { name: 'Select source product' })).toHaveLength(1);
  await userEvent.setup().click(screen.getByRole('button', { name: 'Select source product' }));
  const inspector = screen.getByRole('complementary', { name: 'Selected source' });
  expect(within(inspector).getByRole('heading', { name: 'product' })).toBeTruthy();
  expect(within(inspector).getByText('a'.repeat(40))).toBeTruthy();
  expect(screen.getByLabelText('Current location').textContent).toContain('node=source');
});

it('opens version and attention views through their URL controls without writes', async () => {
  const requests = open({ path: '/composition/overview?view=versions' });
  expect(await screen.findByRole('region', { name: 'Environment version comparison' })).toBeTruthy();
  expect(screen.queryByRole('region', { name: 'Component dependencies' })).toBeNull();
  await userEvent.setup().click(screen.getByRole('button', { name: 'Attention' }));
  expect(screen.getByRole('region', { name: 'Decisions waiting' })).toBeTruthy();
  expect(screen.getByLabelText('Current location').textContent).toBe('?view=attention');
  await userEvent.setup().click(screen.getByRole('button', { name: 'Topology' }));
  expect(screen.getByRole('region', { name: 'Component dependencies' })).toBeTruthy();
  expect(screen.getByLabelText('Current location').textContent).toBe('');
  expect(requests.every(request => request.startsWith('GET '))).toBe(true);
});

it('shows the selected details below the topology and compares pinned nodes side by side', async () => {
  open({ path: '/composition/overview?environment=production&component=api' });
  const details = await screen.findByRole('region', { name: 'Selected details' });
  expect(details.getAttribute('data-cards')).toBe('1');
  expect(within(details).getByRole('complementary', { name: 'Selected component' }).textContent).toContain('api');
  // The one card offers no close: there is nothing to fall back to.
  expect(within(details).queryByRole('button', { name: /^Close/ })).toBeNull();

  const user = userEvent.setup();
  await user.keyboard('{Meta>}');
  await user.click(screen.getByRole('button', { name: 'Select source product' }));
  await user.keyboard('{/Meta}');
  expect(details.getAttribute('data-cards')).toBe('2');
  const pinned = within(details).getByRole('complementary', { name: 'Pinned source product' });
  expect(pinned.className).toContain('compact');
  expect(within(details).getByRole('complementary', { name: 'Selected component' }).className).toContain('compact');
  expect(screen.getByLabelText('Current location').textContent).toBe('?environment=production&component=api&pin=source%3Aproduct');
  expect(screen.getByRole('button', { name: 'Select source product' }).getAttribute('aria-pressed')).toBe('true');

  // ⌘-click on the pinned node again removes it.
  await user.keyboard('{Meta>}');
  await user.click(screen.getByRole('button', { name: 'Select source product' }));
  await user.keyboard('{/Meta}');
  expect(details.getAttribute('data-cards')).toBe('1');

  // Pin again, close it from its card.
  await user.keyboard('{Shift>}');
  await user.click(screen.getByRole('button', { name: 'Select environment production' }));
  await user.keyboard('{/Shift}');
  expect(details.getAttribute('data-cards')).toBe('2');
  await user.click(within(details).getByRole('button', { name: 'Close Pinned environment production' }));
  expect(details.getAttribute('data-cards')).toBe('1');
  expect(screen.getByLabelText('Current location').textContent).toBe('?environment=production&component=api');
});

it('closing the primary card promotes the first pinned one, and a plain click keeps one card', async () => {
  open({ path: '/composition/overview?environment=production&component=api&pin=component%3Aproduction%3Acache&pin=source%3Aproduct' });
  const details = await screen.findByRole('region', { name: 'Selected details' });
  expect(details.getAttribute('data-cards')).toBe('3');
  const user = userEvent.setup();
  await user.click(within(details).getByRole('button', { name: 'Close selected component' }));
  expect(details.getAttribute('data-cards')).toBe('2');
  expect(within(details).getByRole('complementary', { name: 'Selected component' }).textContent).toContain('cache');
  await user.click(screen.getByRole('button', { name: 'Inspect api in production' }));
  expect(details.getAttribute('data-cards')).toBe('1');
  expect(screen.getByLabelText('Current location').textContent).toBe('?environment=production&component=api');
});

it('says the controller is down, why, and that the states shown are as of its last poll', async () => {
  const down = { ...overview, controller: { state: 'down', last_poll: '2026-10-04T14:25:00Z', message: "controller image files corrupted on this node; remove the image from the node's containerd and restart", restarts: 37, status_is_stale: true } };
  open({ snapshot: down as typeof overview });
  const notice = await screen.findByText('GitOps controller is down');
  const box = notice.closest('.notice') as HTMLElement;
  expect(box.className).toContain('danger');
  expect(within(box).getByText(/controller image files corrupted on this node/)).toBeTruthy();
  expect(within(box).getByText(/may be out of date/)).toBeTruthy();
  expect(within(screen.getByLabelText('Composition summary')).getByText('Down')).toBeTruthy();
});

it('shows no controller notice while it runs', async () => {
  open();
  await screen.findByRole('region', { name: 'Selected details' });
  expect(screen.queryByText('GitOps controller is down')).toBeNull();
});
