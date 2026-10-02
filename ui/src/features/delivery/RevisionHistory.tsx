import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api, applicationPath } from '../../api/client';
import type { Operation, PlanRecord } from '../../api/generated';
import { Badge, Failure, Loading, Notice, formatTime } from '../../components/State';
import { DiffWorkbench } from './DiffWorkbench';
import { revisionDiff } from './revisionDiff';
import './revision-history.css';

function RevisionCard({ side, plan, operation }: { side: 'From' | 'To'; plan: PlanRecord; operation: Operation }) {
  return <article className="revision-card" aria-label={`${side} revision`}><header><p className="eyebrow">{side} · {plan.intent}</p><Badge value={operation.state} /></header><h3>{plan.release}</h3><p className="revision-source"><span>{plan.source?.kind === 'git' ? 'Commit' : 'Source revision'}</span><code>{plan.source?.revision ?? 'Not recorded'}</code></p><div className="revision-metadata"><span>{plan.target.name} / {plan.target.namespace || 'Cluster scoped'}</span><time dateTime={operation.created_at}>{formatTime(operation.created_at)}</time></div><footer><Link to={`${applicationPath(plan.application_id)}/changes?plan=${encodeURIComponent(plan.id)}`}>Open {side} plan</Link><Link to={`/runs/${encodeURIComponent(operation.id)}`}>Open {side} run</Link></footer><details><summary>Exact plan identity</summary><code>{plan.id}</code><code>{plan.digest}</code>{plan.source?.entrypoint && <p className="small muted">{plan.source.entrypoint}</p>}</details></article>;
}

function RevisionComparison({ before, after }: { before: PlanRecord; after: PlanRecord }) {
  if (before.target.id !== after.target.id || before.target.namespace !== after.target.namespace || before.target.cluster_uid !== after.target.cluster_uid || before.target.namespace_uid !== after.target.namespace_uid) return <Notice title="Different deployment targets">These plans target different clusters or namespaces. Open each plan to inspect its changes.</Notice>;
  if (!before.desired_resources_complete || !after.desired_resources_complete) return <Notice title="Revision snapshot unavailable">One of these recorded plans has no complete desired configuration snapshot. Its original before/after changes remain available through Open plan. Missing historical fields are not treated as unchanged.</Notice>;
  const diffs = revisionDiff(before.desired_resources ?? [], after.desired_resources ?? []);
  const changed = diffs.filter(diff => ['create', 'delete', 'update'].includes(diff.operation)).length;
  const unknown = diffs.filter(diff => diff.not_compared?.length).length;
  return <section className="revision-comparison" aria-label="Revision differences"><header><div><p className="eyebrow">Desired configuration · From → To</p><h3>{changed} {changed === 1 ? 'resource differs' : 'resources differ'}</h3></div><p className="small muted">{diffs.length} resources compared{unknown ? ` · ${unknown} with unreported fields` : ''}</p></header><p className="small muted">Compares stored desired configuration, independent of current cluster state. Run outcomes above show whether each deployment succeeded. Secret values and other redacted fields are not compared.</p>{diffs.length ? <DiffWorkbench diffs={diffs} comparison /> : <Notice title="No desired resources recorded">Both complete snapshots contain no desired resources.</Notice>}<details className="revision-raw"><summary>Raw revision snapshots</summary><div><pre aria-label="Raw From revision">{JSON.stringify(before.desired_resources, null, 2)}</pre><pre aria-label="Raw To revision">{JSON.stringify(after.desired_resources, null, 2)}</pre></div></details></section>;
}

export function RevisionHistory({ applicationId, operations }: { applicationId: string; operations: Operation[] }) {
  const [params, setParams] = useSearchParams();
  const revisions = useMemo(() => {
    const selected = new Map<string, Operation>();
    [...operations].filter(item => item.application_id === applicationId && item.plan_id).sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)).forEach(item => { if (!selected.has(item.plan_id)) selected.set(item.plan_id, item); });
    return [...selected.values()];
  }, [applicationId, operations]);
  const toId = params.get('compareTo') ?? revisions[0]?.plan_id ?? '';
  const toIndex = revisions.findIndex(run => run.plan_id === toId);
  const fromId = params.get('compareFrom') ?? revisions[toIndex + 1]?.plan_id ?? revisions.find(run => run.plan_id !== toId)?.plan_id ?? toId;
  const fromRun = revisions.find(run => run.plan_id === fromId), toRun = revisions.find(run => run.plan_id === toId);
  const before = useQuery({ queryKey: ['plan', fromId], queryFn: ({ signal }) => api.plan(fromId, signal), enabled: Boolean(fromRun) });
  const after = useQuery({ queryKey: ['plan', toId], queryFn: ({ signal }) => api.plan(toId, signal), enabled: Boolean(toRun) });
  function select(from: string, to: string) { setParams(previous => { const next = new URLSearchParams(previous); next.set('compareFrom', from); next.set('compareTo', to); return next; }); }
  const missing = operations.filter(item => item.application_id === applicationId && !item.plan_id).length;
  const mismatch = (before.data && (before.data.application_id !== applicationId || before.data.id !== fromId || before.data.digest !== fromRun?.approved_digest)) || (after.data && (after.data.application_id !== applicationId || after.data.id !== toId || after.data.digest !== toRun?.approved_digest));
  const ready = before.data && after.data && fromRun && toRun && !before.isError && !after.isError && !mismatch;
  return <section className="revision-history" aria-label="Revision history"><div className="revision-intro"><div><p className="eyebrow">Recorded deployment plans</p><h3>What changed between revisions?</h3></div><p>Choose two recorded plans to trace source, configuration and deployment outcome.</p></div>
    {missing > 0 && <p className="small muted">{missing} {missing === 1 ? 'operation has' : 'operations have'} no stored plan available for revision comparison.</p>}
    {revisions.length === 0 ? <Notice title="No recorded revisions">Deployment plans linked to this application’s operations will appear here.</Notice> : <><div className="revision-selectors">{(['From', 'To'] as const).map(side => { const value = side === 'From' ? fromId : toId; return <label key={side}><span>{side} revision</span><select value={value} onChange={event => select(side === 'From' ? event.target.value : fromId, side === 'To' ? event.target.value : toId)}>{!revisions.some(run => run.plan_id === value) && <option value={value}>Unavailable: {value}</option>}{revisions.map(run => <option key={run.plan_id} value={run.plan_id}>{run.engine_release ?? run.plan_id} · {formatTime(run.created_at)} · {run.state}</option>)}</select></label>; })}<button aria-label="Swap revisions" onClick={() => select(toId, fromId)}>⇄ <span>Swap</span></button></div>
      {(!fromRun || !toRun) ? <Notice title="Selected revision is unavailable">A bookmarked plan is not in the loaded operation history. Select an available revision to continue.</Notice> : <>{(before.isPending || after.isPending) && <Loading text="Loading recorded revisions…" />}{before.isError && <Failure error={before.error} retry={() => void before.refetch()} />}{after.isError && <Failure error={after.error} retry={() => void after.refetch()} />}{mismatch && <Notice title="Plan does not match this history" danger>The returned plan identity, approved digest or application differs from the selected history. Its contents are not displayed.</Notice>}{ready && <><div className="revision-cards"><RevisionCard side="From" plan={before.data!} operation={fromRun} /><div aria-hidden="true" className="revision-direction">→</div><RevisionCard side="To" plan={after.data!} operation={toRun} /></div>{fromId === toId ? <Notice title="Select two different revisions">Choose another recorded plan to inspect configuration changes.</Notice> : <RevisionComparison before={before.data!} after={after.data!} />}</>}</>}
    </>}
  </section>;
}
