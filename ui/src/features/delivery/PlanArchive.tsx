import { useEffect, useMemo, useState } from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api, applicationPath } from '../../api/client';
import type { Application, Operation, PlanSummary } from '../../api/generated';
import { Badge, Failure, Loading, Notice, formatTime } from '../../components/State';
import './plan-archive.css';

const timestamp = (value: string) => Date.parse(value) || 0;
const runIdentity = (application: string, plan: string, digest: string) => JSON.stringify([application, plan, digest]);
function expiry(plan: PlanSummary, now: number) {
  const deadline = Date.parse(plan.expires_at);
  return !Number.isFinite(deadline) ? 'unknown' : deadline <= now ? 'expired' : 'unexpired';
}
function useArchiveTime(plans: PlanSummary[]) {
  const [now, setNow] = useState(Date.now);
  const next = plans.reduce((earliest, plan) => { const value = Date.parse(plan.expires_at); return value > now && value < earliest ? value : earliest; }, Infinity);
  useEffect(() => {
    if (!Number.isFinite(next)) return;
    const timer = window.setTimeout(() => setNow(Date.now()), Math.min(Math.max(0, next - Date.now()) + 1, 2_147_000_000));
    return () => window.clearTimeout(timer);
  }, [next, now]);
  return now;
}

function ArchiveRow({ plan, runs, runState, partialRuns, now }: { plan: PlanSummary; runs: Operation[]; runState: 'loading' | 'unavailable' | 'ready'; partialRuns: boolean; now: number }) {
  const latest = runs[0];
  const review = `${applicationPath(plan.application_id)}/changes?plan=${encodeURIComponent(plan.id)}`;
  const state = expiry(plan, now);
  const revision = plan.source?.revision;
  return <tr aria-label={`Plan ${plan.id}`}>
    <td data-label="Recorded"><time dateTime={plan.created_at}>{formatTime(plan.created_at)}</time><span className="archive-intent">{plan.intent} · {plan.plan_kind?.replaceAll('-', ' ') ?? 'release'}</span></td>
    <td data-label="Plan"><Link className="archive-release" to={review}>{plan.release || 'Release not reported'}</Link><code className="archive-id" title={plan.id}>{plan.id}</code><details className="archive-identity"><summary>Exact identity</summary><dl><dt>Plan</dt><dd><code>{plan.id}</code></dd><dt>Approval digest</dt><dd><code>{plan.digest}</code></dd><dt>Target</dt><dd>{plan.target.name} / {plan.target.namespace || 'Cluster scope'}</dd>{plan.source?.entrypoint && <><dt>Entrypoint</dt><dd><code>{plan.source.entrypoint}</code></dd></>}</dl></details></td>
    <td data-label="Revision"><span className="archive-source-kind">{plan.source?.kind ?? 'Source unreported'}</span>{revision ? <code title={revision}>{revision.length > 16 ? `${revision.slice(0, 12)}…` : revision}</code> : <span className="muted">Not recorded</span>}{revision && <details className="archive-identity"><summary>Full revision</summary><code>{revision}</code></details>}</td>
    <td data-label="Actions"><div className="archive-action-counts">{Object.entries(plan.summary).map(([action, count]) => <span key={action} data-action={action}><strong>{count}</strong> {action}</span>)}</div>{Object.keys(plan.summary).length === 0 && <span className="muted">Not reported</span>}</td>
    <td data-label="Approval expiry"><span className={`archive-expiry ${state}`}>{state === 'expired' ? 'Expired' : state === 'unexpired' ? 'Unexpired' : 'Unknown expiry'}</span><time dateTime={plan.expires_at}>{formatTime(plan.expires_at)}</time></td>
    <td data-label="Recorded runs">{runState !== 'ready' ? <span className="muted">{runState === 'loading' ? 'Checking run history…' : 'Run history unavailable'}</span> : latest ? <><Badge value={latest.state} /><div className="archive-run-links"><Link to={`/runs/${encodeURIComponent(latest.id)}`}>Open run</Link><Link to={`/runs/${encodeURIComponent(latest.id)}?runView=logs#execution-journal`}>Open logs</Link></div>{runs.length > 1 && <details className="archive-identity"><summary>{runs.length} recorded attempts</summary><ul>{runs.map(run => <li key={run.id}><Link to={`/runs/${encodeURIComponent(run.id)}`}>{run.id}</Link> · {run.state} · {formatTime(run.created_at)}</li>)}</ul></details>}</> : <><span className="archive-prepared">Prepared</span><span className="archive-no-run">{partialRuns ? 'No matching run loaded' : 'No recorded run'}</span></>}</td>
    <td className="archive-open"><Link className="button" to={review}>Open plan</Link></td>
  </tr>;
}

/** Stored plans only: opening this archive never evaluates a source or renews approval. */
export function PlanArchive({ application }: { application: Application }) {
  const [params, setParams] = useSearchParams();
  const canRead = application.capabilities?.activity?.allowed === true;
  const plans = useInfiniteQuery({ queryKey: ['plans', application.id], initialPageParam: undefined as string | undefined, queryFn: ({ pageParam, signal }) => api.plans(application.id, pageParam, signal), getNextPageParam: page => page.next_page ?? undefined, enabled: canRead });
  const operations = useQuery({ queryKey: ['operations', application.id], queryFn: ({ signal }) => api.operations(application.id, signal), enabled: canRead });
  const items = useMemo(() => [...new Map((plans.data?.pages.flatMap(page => page.items) ?? []).filter(plan => plan.application_id === application.id).map(plan => [plan.id, plan])).values()], [plans.data, application.id]);
  const mismatched = plans.data?.pages.some(page => page.items.some(plan => plan.application_id !== application.id));
  const runs = useMemo(() => {
    const result = new Map<string, Operation[]>();
    for (const run of [...(operations.data?.items ?? [])].sort((a, b) => timestamp(b.created_at) - timestamp(a.created_at))) {
      if (run.application_id !== application.id) continue;
      const identity = runIdentity(run.application_id, run.plan_id, run.approved_digest);
      result.set(identity, [...(result.get(identity) ?? []), run]);
    }
    return result;
  }, [operations.data, application.id]);
  const now = useArchiveTime(items);
  const searchValue = params.get('planSearch') ?? '';
  const search = searchValue.trim().toLowerCase();
  const filter = params.get('planFilter') ?? 'all';
  const sort = params.get('planSort') ?? 'newest';
  const runState = operations.isError ? 'unavailable' : operations.data ? 'ready' : 'loading';
  const selectedRuns = (plan: PlanSummary) => runs.get(runIdentity(plan.application_id, plan.id, plan.digest)) ?? [];
  const visible = items.filter(plan => {
    const matches = `${plan.id} ${plan.release} ${plan.digest} ${plan.source?.revision ?? ''} ${plan.source?.entrypoint ?? ''} ${plan.intent} ${plan.target.name} ${plan.target.namespace}`.toLowerCase().includes(search);
    const matchesFilter = filter === 'expired' || filter === 'unexpired' ? expiry(plan, now) === filter : filter === 'with-runs' ? runState === 'ready' && selectedRuns(plan).length > 0 : filter === 'no-runs' ? runState === 'ready' && selectedRuns(plan).length === 0 : true;
    return matches && matchesFilter;
  }).sort((a, b) => sort === 'oldest' ? timestamp(a.created_at) - timestamp(b.created_at) || a.id.localeCompare(b.id) : sort === 'expiry' ? timestamp(a.expires_at) - timestamp(b.expires_at) || a.id.localeCompare(b.id) : timestamp(b.created_at) - timestamp(a.created_at) || a.id.localeCompare(b.id));
  function update(key: string, value: string, fallback = '') { setParams(previous => { const next = new URLSearchParams(previous); if (value && value !== fallback) next.set(key, value); else next.delete(key); return next; }, { replace: true }); }
  if (!canRead) return <Notice title="Plan history unavailable">{application.capabilities?.activity?.reason ?? 'This session cannot read this application’s recorded plans.'}</Notice>;
  return <section className="plan-archive" aria-label="Recorded plan archive"><header className="archive-heading"><div><p className="eyebrow">Saved review evidence</p><h2>Plans</h2><p>Find every plan recorded by this service, including expired reviews and plans with no recorded run.</p></div><button disabled={plans.isFetching || operations.isFetching} onClick={() => { void plans.refetch(); void operations.refetch(); }}>Refresh plans</button></header>
    <div className="archive-tools"><label className="archive-search">Search recorded plans<input type="search" value={searchValue} onChange={event => update('planSearch', event.target.value)} placeholder="Release, revision, plan or digest…" /></label><label>Filter plans<select value={filter} onChange={event => update('planFilter', event.target.value, 'all')}><option value="all">All plans</option><option value="unexpired">Unexpired</option><option value="expired">Expired</option><option value="with-runs">With recorded runs</option><option value="no-runs">No recorded run</option></select></label><label>Sort plans<select value={sort} onChange={event => update('planSort', event.target.value, 'newest')}><option value="newest">Newest first</option><option value="oldest">Oldest first</option><option value="expiry">Approval expiry</option></select></label></div>
    <div className="archive-scope"><p aria-live="polite">{visible.length} of {items.length} loaded plans{plans.hasNextPage ? ' · More recorded plans available' : ''}</p><p>Search and sort apply to loaded pages. Expiry describes the approval window, not deployment authority.</p></div>
    {plans.isPending && <Loading text="Loading recorded plans…" />}{plans.isError && <Failure error={plans.error} retry={() => void plans.refetch()} />}{operations.isError && <Notice title="Run history unavailable">Plan evidence remains available. Run links cannot be resolved until operation history can be read.</Notice>}{mismatched && <Notice title="Some records do not match this application" danger>Records from another application are excluded from this archive.</Notice>}
    {plans.data && (items.length === 0 ? <div className="panel empty"><h3>No recorded plans</h3><p>Plans appear after source evaluation completes. Opening this archive does not create or approve one.</p></div> : visible.length === 0 ? <div className="panel empty"><h3>No loaded plans match these filters</h3><p>Adjust the search, choose All plans, or load another page.</p><button onClick={() => { setParams(previous => { const next = new URLSearchParams(previous); next.delete('planSearch'); next.delete('planFilter'); return next; }, { replace: true }); }}>Clear plan filters</button></div> : <div className="archive-table-wrap"><table aria-label="Recorded plans"><thead><tr><th scope="col">Recorded</th><th scope="col">Plan / release</th><th scope="col">Source revision</th><th scope="col">Actions</th><th scope="col">Approval expiry</th><th scope="col">Recorded runs</th><th scope="col"><span className="archive-screenreader">Open review</span></th></tr></thead><tbody>{visible.map(plan => <ArchiveRow key={plan.id} plan={plan} runs={selectedRuns(plan)} runState={runState} partialRuns={Boolean(operations.data?.next_page)} now={now} />)}</tbody></table></div>)}
    {plans.hasNextPage && <div className="archive-pagination"><p>Older plans remain available without re-evaluating their source.</p><button disabled={plans.isFetchingNextPage} onClick={() => void plans.fetchNextPage()}>{plans.isFetchingNextPage ? 'Loading older plans…' : 'Load more plans'}</button></div>}
  </section>;
}
