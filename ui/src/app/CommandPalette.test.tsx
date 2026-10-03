import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { CommandPalette } from './CommandPalette';
import type { Capabilities } from '../api/generated';

const capabilities: Capabilities = { principal: { id: 'local', name: 'Operator' }, targets: [], actions: { composition: { allowed: true } } };
function Address() { return <output aria-label="Address">{useLocation().pathname}{useLocation().search}</output>; }
function mount(allowed = capabilities) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const fetch = vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    if (path.endsWith('/composition')) return Response.json({ environments: [{ name: 'stage/review', namespace: 'shop-stage', components: [{ name: 'api worker', source: 'product' }] }] });
    return Response.json({ items: [{ id: 'shop', name: 'Shop', target: { name: 'east', namespace: 'shop' } }], cursor: 'applications' });
  });
  vi.stubGlobal('fetch', fetch);
  render(<QueryClientProvider client={client}><MemoryRouter><CommandPalette capabilities={allowed} /><Address /><main id="main" tabIndex={-1} /></MemoryRouter></QueryClientProvider>);
  return fetch;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('loads destinations only when opened and restores focus on Escape', async () => {
  const fetch = mount();
  const user = userEvent.setup();
  expect(fetch).not.toHaveBeenCalled();
  const trigger = screen.getByRole('button', { name: 'Search workspace' });
  await user.click(trigger);
  await screen.findByRole('link', { name: /Component: api worker/ });
  expect(screen.getByRole('searchbox', { name: 'Search destinations' })).toBe(document.activeElement);
  await user.keyboard('{Escape}');
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  await waitFor(() => expect(document.activeElement).toBe(trigger));
});

it('jumps to the exact environment/component and preserves encoded identities', async () => {
  mount();
  const user = userEvent.setup();
  await user.keyboard('{Control>}k{/Control}');
  await user.type(await screen.findByRole('searchbox', { name: 'Search destinations' }), 'api worker');
  await user.click(await screen.findByRole('link', { name: /Component: api worker/ }));
  const url = new URL(screen.getByLabelText('Address').textContent!, 'http://localhost');
  expect(url.pathname).toBe('/composition/overview');
  expect(url.searchParams.get('environment')).toBe('stage/review');
  expect(url.searchParams.get('component')).toBe('api worker');
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('main')));
});

it('does not offer forbidden pages or read composition data for inventory sessions', async () => {
  const fetch = mount({ ...capabilities, actions: { inspect: { allowed: true } } });
  await userEvent.setup().click(screen.getByRole('button', { name: 'Search workspace' }));
  await screen.findByRole('link', { name: /Application: Shop/ });
  expect(screen.queryByRole('link', { name: /Infrastructure|Versions|Attention|Pipeline|Cluster build|GitOps/ })).toBeNull();
  expect(fetch.mock.calls.some(([request]) => request.url.includes('/composition'))).toBe(false);
  expect(fetch.mock.calls.every(([request]) => request.method === 'GET')).toBe(true);
});
