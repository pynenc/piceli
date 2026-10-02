import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { ApplicationEnvironmentContext } from './ApplicationEnvironmentContext';

const composition = { configured: true, environments: [
  { name: 'review/one', namespace: 'shop', application_id: 'app/shop', components: [{ name: 'api/web' }] },
  { name: 'unrelated', namespace: 'shop', application_id: 'other-app', components: [{ name: 'api/web' }] },
] };
function open(allowed = true, applicationId = 'app/shop', cached = false) {
  const fetch = vi.fn(async () => Response.json(composition)); vi.stubGlobal('fetch', fetch);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  if (cached) client.setQueryData(['composition'], composition);
  render(<QueryClientProvider client={client}><MemoryRouter><ApplicationEnvironmentContext applicationId={applicationId} allowed={allowed} /></MemoryRouter></QueryClientProvider>);
  return fetch;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('connects runtime to the exact published environment and component identities', async () => {
  open();
  const nav = await screen.findByRole('navigation', { name: 'Related infrastructure' });
  const environment = within(nav).getByRole('link', { name: 'review/one' });
  expect(new URL(environment.getAttribute('href')!, 'http://test').searchParams.get('environment')).toBe('review/one');
  const component = within(nav).getByRole('link', { name: 'Inspect api/web in review/one' });
  const url = new URL(component.getAttribute('href')!, 'http://test');
  expect(url.searchParams.get('component')).toBe('api/web');
  expect(url.searchParams.get('environment')).toBe('review/one');
  const compare = new URL(within(nav).getByRole('link', { name: 'Compare versions →' }).getAttribute('href')!, 'http://test');
  expect(compare.searchParams.get('baseline')).toBe('review/one');
  expect(screen.queryByText('unrelated')).toBeNull();
});

it('does not infer a relationship from a matching namespace or component name', async () => {
  const fetch = open(true, 'shop');
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
  expect(screen.queryByRole('navigation', { name: 'Related infrastructure' })).toBeNull();
});

it('makes no composition request or cached disclosure without its capability', () => {
  const fetch = open(false, 'app/shop', true);
  expect(fetch).not.toHaveBeenCalled();
  expect(screen.queryByRole('navigation', { name: 'Related infrastructure' })).toBeNull();
});
