import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import type { Component, Environment, Source } from './Composition';
import { CompositionVersions, compareComponentVersions } from './CompositionVersions';

const before = 'a'.repeat(40);
const after = 'b'.repeat(40);
const component: Component = { name: 'api', source: 'product', commit: before, digest: `sha256:${'c'.repeat(64)}`, state: 'synced', health: 'healthy' };
const sources: Source[] = [{ name: 'product', url: 'git@github.com:example/product.git', refs: { main: after, production: before, short: before.slice(0, 7) } }];
const environments: Environment[] = [
  { name: 'production', state: 'deployed', health: 'healthy', revision: {}, components: [component, { ...component, name: 'worker' }] },
  { name: 'staging', state: 'deployed', health: 'healthy', revision: {}, components: [{ ...component, commit: after }, { ...component, name: 'worker' }] },
];
function Address() { return <output aria-label="Version address">{useLocation().search}</output>; }
function open(path = '/composition/overview?view=versions&baseline=production&compare=staging&focus=api') {
  const onInspect = vi.fn();
  render(<MemoryRouter initialEntries={[path]}><CompositionVersions environments={environments} sources={sources} onInspect={onInspect} /><Address /></MemoryRouter>);
  return onInspect;
}
afterEach(cleanup);

it('restores a focused counterpart comparison, swaps identities and can return to all components', async () => {
  const onInspect = open();
  expect(screen.getAllByRole('rowheader')).toHaveLength(1);
  expect((screen.getByRole('combobox', { name: 'Focus component' }) as HTMLSelectElement).value).toBe('api');
  const row = screen.getByRole('rowheader', { name: /api/ }).closest('tr')!;
  await userEvent.setup().click(within(row).getByRole('button', { name: 'Inspect baseline →' }));
  expect(onInspect).toHaveBeenCalledWith('production', 'api');
  await userEvent.setup().click(screen.getByRole('button', { name: 'Swap environments' }));
  expect(screen.getByLabelText('Version address').textContent).toBe('?view=versions&baseline=staging&compare=production&focus=api');
  expect(within(row).getByRole('link', { name: /Compare in GitHub/ }).getAttribute('href')).toBe(`https://github.com/example/product/compare/${after}...${before}`);
  await userEvent.setup().click(screen.getByRole('button', { name: 'Show all components' }));
  expect(screen.getAllByRole('rowheader')).toHaveLength(2);
  expect(screen.getByLabelText('Version address').textContent).not.toContain('focus=');
});

it('connects each known source and exact commit to their provenance, matching only full refs', () => {
  open();
  const row = screen.getByRole('rowheader', { name: /api/ }).closest('tr')!;
  const baseline = within(within(row).getAllByRole('cell')[0]);
  expect(baseline.getByRole('link', { name: 'Inspect source product' }).getAttribute('href')).toBe('/composition/overview?environment=production&node=source&source=product');
  expect(baseline.getByRole('link', { name: 'View api commit in GitHub' }).getAttribute('href')).toBe(`https://github.com/example/product/commit/${before}`);
  expect(baseline.getByText('production')).toBeTruthy();
  expect(baseline.queryByText('short')).toBeNull();
  expect(baseline.queryByText('main')).toBeNull();
});

it('keeps a removed focused component explicit instead of silently showing another comparison', async () => {
  open('/composition/overview?view=versions&baseline=production&compare=staging&focus=removed');
  expect(screen.getByText('Focused component unavailable')).toBeTruthy();
  expect(screen.queryByRole('rowheader')).toBeNull();
  await userEvent.setup().click(screen.getByRole('button', { name: 'Show all components' }));
  expect(screen.getAllByRole('rowheader')).toHaveLength(2);
});

it('does not classify abbreviated commits as exact equality or ordering evidence', () => {
  expect(compareComponentVersions({ ...component, commit: before.slice(0, 7) }, { ...component, commit: before.slice(0, 7) })).toMatchObject({ commit: 'unknown', state: 'unknown' });
  expect(compareComponentVersions(component, { ...component, commit: before.toUpperCase() })).toMatchObject({ commit: 'same', state: 'same' });
});
