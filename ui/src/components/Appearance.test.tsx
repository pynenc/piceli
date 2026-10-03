import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Appearance } from './Appearance';

beforeEach(() => { localStorage.clear(); delete document.documentElement.dataset.theme; });
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); delete document.documentElement.dataset.theme; });

it('keeps an explicit appearance across remounts and lets the user return to System', async () => {
  const user = userEvent.setup();
  const first = render(<Appearance />);
  expect(document.documentElement.dataset.theme).toBe('system');
  await user.selectOptions(screen.getByRole('combobox', { name: 'Appearance' }), 'dark');
  expect(document.documentElement.dataset.theme).toBe('dark');
  first.unmount();
  render(<Appearance />);
  expect((screen.getByRole('combobox', { name: 'Appearance' }) as HTMLSelectElement).value).toBe('dark');
  await user.selectOptions(screen.getByRole('combobox', { name: 'Appearance' }), 'system');
  expect(document.documentElement.dataset.theme).toBe('system');
  expect(localStorage.getItem('piceli.appearance')).toBe('system');
});

it('still allows a session preference when browser storage is unavailable', async () => {
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('Blocked'); });
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('Blocked'); });
  render(<Appearance />);
  await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'Appearance' }), 'light');
  expect(document.documentElement.dataset.theme).toBe('light');
});
