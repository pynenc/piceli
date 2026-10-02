import { afterEach, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { ApplicationConnections } from './ApplicationConnections';
import { FleetStatus, matchesFleetFilter } from './FleetStatus';
import type { Application } from '../api/generated';

const app: Application = { id: 'shop/demo', name: 'Shop', target: { id: 'east', name: 'east', namespace: 'shop' }, definition_kind: 'inventory', ownership: 'inventory', freshness: { state: 'connected' } };
afterEach(cleanup);

it('keeps unknown observations distinct from failures and reported changes', () => {
  expect(matchesFleetFilter(app, 'unknown')).toBe(true);
  expect(matchesFleetFilter(app, 'attention')).toBe(false);
  expect(matchesFleetFilter(app, 'changes')).toBe(false);
  expect(matchesFleetFilter({ ...app, health: 'healthy', freshness: { state: 'stale' } }, 'unknown')).toBe(true);
  expect(matchesFleetFilter({ ...app, health: 'degraded' }, 'attention')).toBe(true);
  expect(matchesFleetFilter({ ...app, health: 'healthy', operation: 'failed' }, 'attention')).toBe(true);
  expect(matchesFleetFilter({ ...app, relation: 'drifted' }, 'changes')).toBe(true);
});

it('counts reported flags and lets the operator choose an attention filter', async () => {
  let selection = '';
  render(<FleetStatus applications={[app, { ...app, id: 'api', health: 'degraded' }]} filter="" select={value => { selection = value; }} />);
  const attention = screen.getByRole('button', { name: /Needs attention/ });
  expect(attention.textContent).toContain('1');
  await userEvent.setup().click(attention);
  expect(selection).toBe('attention');
});

it('includes cached healthy applications in the stale filter after connection loss', () => {
  const healthy: Application = { ...app, health: 'healthy' };
  expect(matchesFleetFilter(healthy, 'unknown', true)).toBe(true);
  expect(matchesFleetFilter(healthy, 'attention', true)).toBe(false);
  render(<FleetStatus applications={[healthy]} filter="unknown" disconnected select={() => {}} />);
  expect(screen.getByRole('button', { name: /Unknown or stale/ }).textContent).toContain('1');
});

it('links only available review/history actions and always keeps the scoped resource graph', () => {
  const view = render(<MemoryRouter><ApplicationConnections application={app} /></MemoryRouter>);
  expect(screen.getByRole('link', { name: /Explore resource graph/ }).getAttribute('href')).toBe('/applications/shop%2Fdemo/resources?view=relationships');
  expect(screen.queryByRole('link', { name: /Review changes|Previous plans|Runs & logs|Compare revisions/ })).toBeNull();
  view.rerender(<MemoryRouter><ApplicationConnections application={{ ...app, capabilities: { evaluate: { allowed: true }, activity: { allowed: true } } }} /></MemoryRouter>);
  expect(screen.getByRole('link', { name: /Review changes/ }).getAttribute('href')).toBe('/applications/shop%2Fdemo/changes');
  expect(screen.getByRole('link', { name: /Runs & logs/ }).getAttribute('href')).toBe('/applications/shop%2Fdemo/activity');
});
