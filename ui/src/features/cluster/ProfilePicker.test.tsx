import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ProfilePicker } from './ProfilePicker';

function open(refused = false) {
  const posts: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    if (request.method === 'POST') {
      posts.push(await request.json());
      return refused ? Response.json({ code: 'forbidden', message: 'Unavailable profile.', correlation_id: 'test' }, { status: 403 }) : Response.json({ state: 'restarting' });
    }
    return Response.json({ active: 'development', profiles: [{ name: 'development', available: true }, { name: 'staging', available: true }, { name: 'unconfigured', available: false }] });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ProfilePicker /></QueryClientProvider>);
  return posts;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('requires an explicit switch and keeps unavailable profiles disabled', async () => {
  const posts = open();
  const picker = await screen.findByRole('combobox', { name: 'Choose credential profile' });
  const button = screen.getByRole('button', { name: 'Switch' });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('option', { name: 'unconfigured (unavailable)' }) as HTMLOptionElement).disabled).toBe(true);
  const user = userEvent.setup();
  await user.selectOptions(picker, 'staging');
  expect(posts).toEqual([]);
  await user.click(button);
  expect(posts).toEqual([{ name: 'staging' }]);
  expect((await screen.findByRole('status')).textContent).toContain('Reopen the new local launch address');
  expect((picker as HTMLSelectElement).disabled).toBe(true);
  expect((button as HTMLButtonElement).disabled).toBe(true);
});

it('explains a refused switch and leaves the selected profile available to retry', async () => {
  const posts = open(true);
  const picker = await screen.findByRole('combobox', { name: 'Choose credential profile' });
  const user = userEvent.setup();
  await user.selectOptions(picker, 'staging');
  await user.click(screen.getByRole('button', { name: 'Switch' }));
  expect((await screen.findByRole('alert')).textContent).toContain('piceli profiles --json');
  expect(screen.queryByRole('status')).toBeNull();
  expect((picker as HTMLSelectElement).value).toBe('staging');
  expect((screen.getByRole('button', { name: 'Switch' }) as HTMLButtonElement).disabled).toBe(false);
  expect(posts).toHaveLength(1);
});
