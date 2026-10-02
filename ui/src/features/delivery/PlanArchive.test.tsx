import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';
import type { Application, Operation } from '../../api/generated';
import { PlanArchive } from './PlanArchive';

const application: Application = { id: 'shop/main', name: 'Shop', target: { id: 'cluster', name: 'Production', namespace: 'shop' }, definition_kind: 'release', ownership: 'native', freshness: { state: 'connected' }, capabilities: { activity: { allowed: true }, evaluate: { allowed: false }, deploy: { allowed: false } } };
const base = { application_id: application.id, target: application.target, source: { kind: 'git', revision: 'a'.repeat(40), entrypoint: 'app.py' }, intent: 'deploy', plan_kind: 'release', desired_resources_complete: true, summary: { create: 2, update: 1 }, expires_at: '2099-01-01T00:00:00Z' };
const prepared = { ...base, id: 'prepared/1', digest: 'a'.repeat(64), release: 'release-prepared', created_at: '2026-10-02T12:00:00Z' };
const expired = { ...base, id: 'expired-2', digest: 'b'.repeat(64), release: 'release-expired', created_at: '2026-10-01T12:00:00Z', expires_at: '2020-01-01T00:00:00Z' };
const older = { ...base, id: 'old-3', digest: 'c'.repeat(64), release: 'release-old', created_at: '2026-09-30T12:00:00Z', source: null };
const operation: Operation = { id: 'matched/run', application_id: application.id, plan_id: expired.id, approved_digest: expired.digest, state: 'succeeded', actor: 'operator', trigger: 'ui', created_at: expired.created_at, updated_at: expired.created_at };
function Address() { return <output aria-label="Archive address">{useLocation().search}</output>; }
function open(path = '/applications/shop%2Fmain/plans?keep=context', app = application, errorHistory = false, partialHistory = false, foreignPlan = false) {
  const requests: { path: string; query: string; method: string }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
    const url = new URL(request.url);
    requests.push({ path: url.pathname, query: url.search, method: request.method });
    if (url.pathname.endsWith('/plans')) return Response.json(url.searchParams.has('page') ? { items: [older], cursor: 'archive', next_page: null } : { items: [expired, prepared, ...(foreignPlan ? [{ ...older, application_id: 'different-app' }] : [])], cursor: 'archive', next_page: 'older/opaque cursor' });
    if (url.pathname.endsWith('/operations')) return errorHistory ? Response.json({ code: 'unavailable', message: 'History unavailable', correlation_id: 'test' }, { status: 503 }) : Response.json({ items: [operation, { ...operation, id: 'wrong-digest', plan_id: prepared.id, approved_digest: 'wrong' }, { ...operation, id: 'wrong-app', plan_id: prepared.id, approved_digest: prepared.digest, application_id: 'other' }], cursor: 'runs', next_page: partialHistory ? 'more-runs' : null });
    return Response.json({ code: 'unknown', message: 'Unexpected request', correlation_id: 'test' }, { status: 404 });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><PlanArchive application={app} /><Address /></MemoryRouter></QueryClientProvider>);
  return requests;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

it('lists prepared and expired plans with exact scope links and joins only matching run approvals', async () => {
  const requests = open();
  const preparedRow = within(await screen.findByRole('row', { name: `Plan ${prepared.id}` }));
  const expiredRow = within(screen.getByRole('row', { name: `Plan ${expired.id}` }));
  expect(preparedRow.getByRole('link', { name: 'Open plan' }).getAttribute('href')).toBe('/applications/shop%2Fmain/changes?plan=prepared%2F1');
  expect(await preparedRow.findByText('No recorded run')).toBeTruthy();
  expect(preparedRow.getByText('Prepared')).toBeTruthy();
  expect(preparedRow.queryByRole('link', { name: /Open run/ })).toBeNull();
  expect(expiredRow.getByText('Expired')).toBeTruthy();
  expect(expiredRow.getByRole('link', { name: 'Open run' }).getAttribute('href')).toBe('/runs/matched%2Frun');
  expect(expiredRow.getByRole('link', { name: 'Open logs' }).getAttribute('href')).toBe('/runs/matched%2Frun?runView=logs#execution-journal');
  expect(expiredRow.getByRole('link', { name: 'Open plan' })).toBeTruthy();
  expect(screen.queryByText(/undeployed/i)).toBeNull();
  expect(screen.queryByRole('button', { name: /approve|deploy/i })).toBeNull();
  expect(requests.every(request => request.method === 'GET')).toBe(true);
});

it('loads additional pages explicitly and keeps scope, search, status and sort in the URL', async () => {
  const requests = open();
  await screen.findByRole('row', { name: `Plan ${prepared.id}` });
  expect(screen.queryByRole('row', { name: `Plan ${older.id}` })).toBeNull();
  await userEvent.setup().click(screen.getByRole('button', { name: 'Load more plans' }));
  await screen.findByRole('row', { name: `Plan ${older.id}` });
  expect(requests.some(request => new URLSearchParams(request.query).get('page') === 'older/opaque cursor')).toBe(true);
  await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'Sort plans' }), 'oldest');
  expect(screen.getAllByRole('row').filter(row => row.getAttribute('aria-label')?.startsWith('Plan ')).map(row => row.getAttribute('aria-label'))).toEqual(['Plan old-3', 'Plan expired-2', 'Plan prepared/1']);
  await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'Filter plans' }), 'expired');
  expect(screen.getAllByRole('row').filter(row => row.getAttribute('aria-label')?.startsWith('Plan '))).toHaveLength(1);
  await userEvent.setup().type(screen.getByRole('searchbox', { name: 'Search recorded plans' }), 'release-expired');
  expect(screen.getByLabelText('Archive address').textContent).toContain('keep=context');
  expect(screen.getByLabelText('Archive address').textContent).toContain('planSort=oldest');
  expect(screen.getByLabelText('Archive address').textContent).toContain('planFilter=expired');
  expect(screen.getByLabelText('Archive address').textContent).toContain('planSearch=release-expired');
});

it('restores an archive search bookmark without treating absent matches as an empty archive', async () => {
  open('/applications/shop%2Fmain/plans?planSearch=missing&planFilter=expired');
  expect(await screen.findByText('No loaded plans match these filters')).toBeTruthy();
  expect(screen.getByText(/0 of 2 loaded plans/)).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Load more plans' })).toBeTruthy();
  expect(screen.queryByText('No recorded plans')).toBeNull();
});

it('keeps plan evidence visible when run history is unavailable without claiming no execution', async () => {
  open(undefined, application, true);
  await screen.findByRole('row', { name: `Plan ${prepared.id}` });
  await waitFor(() => expect(screen.getAllByText('Run history unavailable').length).toBeGreaterThan(0));
  expect(within(screen.getByRole('table', { name: 'Recorded plans' })).queryByText('No recorded run')).toBeNull();
  expect(screen.getAllByRole('link', { name: 'Open plan' })).toHaveLength(2);
});

it('does not fetch stored plans or runs when the application denies history access', async () => {
  const requests = open(undefined, { ...application, capabilities: { activity: { allowed: false, reason: 'History denied' } } });
  expect(screen.getByText('Plan history unavailable')).toBeTruthy();
  await waitFor(() => expect(requests).toHaveLength(0));
});

it('limits missing-run claims to the loaded history when additional operation pages exist', async () => {
  open(undefined, application, false, true);
  const row = within(await screen.findByRole('row', { name: `Plan ${prepared.id}` }));
  expect(await row.findByText('No matching run loaded')).toBeTruthy();
  expect(row.queryByText('No recorded run')).toBeNull();
  expect(row.queryByRole('link', { name: 'Open run' })).toBeNull();
});

it('excludes unexpected plan records from another application before rendering their evidence or links', async () => {
  open(undefined, application, false, false, true);
  expect(await screen.findByText('Some records do not match this application')).toBeTruthy();
  expect(screen.queryByRole('row', { name: `Plan ${older.id}` })).toBeNull();
  expect(screen.queryByText(older.release)).toBeNull();
});
