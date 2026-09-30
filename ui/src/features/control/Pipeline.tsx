import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../../api/client';
import { Badge, Failure, Loading, Notice } from '../../components/State';
import { requestKey } from '../delivery/requests';

type Stage = { name: string; state?: string; action?: string; why?: string; summary?: Record<string, number>; changes?: Array<{ operation: string; kind: string; name: string }>; checks?: unknown[]; claims?: unknown[]; writers?: unknown[]; release?: string; rollback_on_failed_checks?: boolean };
type Plan = { id: string; digest: string; materialized: boolean; phase: 'preliminary' | 'final'; expires_at: string; target: { name: string; namespace: string }; stages: Stage[] };
type Run = { id: string; state: string; plan_id: string; next_plan_id?: string; stages: Record<string, string>; error_code?: string; runner_id?: string; created_at: string };
const asPlan = (value: unknown) => value as Plan;
const asRun = (value: unknown) => value as Run;
const label = (name: string) => name === 'prerollout' ? 'Pre-rollout checks' : name === 'backup' ? 'Restore point' : name[0].toUpperCase() + name.slice(1);

function Stages({ stages }: { stages: Stage[] }) {
  return <ol className="pipeline-stages">{stages.map(stage => <li key={stage.name}><h3>{label(stage.name)} <Badge value={stage.state ?? stage.action ?? 'planned'} /></h3>{stage.why && <p className="small muted">{stage.why}</p>}{stage.summary && <p className="small muted">{Object.entries(stage.summary).map(([key, count]) => `${count} ${key}`).join(' · ')}</p>}{stage.changes?.length ? <ul>{stage.changes.map((change, index) => <li key={`${change.kind}/${change.name}/${index}`}>{change.operation} {change.kind}/{change.name}</li>)}</ul> : null}{stage.checks?.length ? <details><summary>{stage.checks.length} declared checks</summary><pre>{JSON.stringify(stage.checks, null, 2)}</pre></details> : null}{stage.claims?.length ? <details><summary>{stage.claims.length} claims in restore point</summary><pre>{JSON.stringify(stage.claims, null, 2)}</pre></details> : null}{stage.writers?.length ? <p className="small muted">Writers will be quiesced before backup.</p> : null}</li>)}</ol>;
}

export function Pipeline() {
  const [params, setParams] = useSearchParams();
  const planId = params.get('plan');
  const runId = params.get('run');
  const [confirmed, setConfirmed] = useState(false);
  const [confirmedSecond, setConfirmedSecond] = useState(false);
  const client = useQueryClient();
  const prepared = useMutation({ mutationFn: () => api.pipelinePlan().then(asPlan), onSuccess: plan => { setConfirmed(false); setParams({ plan: plan.id }); } });
  const plan = useQuery({ queryKey: ['pipeline-plan', planId], queryFn: ({ signal }) => api.pipelineGetPlan(planId!, signal).then(asPlan), enabled: Boolean(planId) && !runId });
  const admit = useMutation({ mutationFn: (value: Plan) => api.pipelineAdmit({ plan_id: value.id, approved_digest: value.digest, idempotency_key: requestKey('pipeline', `${value.id}:${value.digest}`) }).then(asRun), onSuccess: run => { void client.invalidateQueries({ queryKey: ['pipeline-operations'] }); setParams({ run: run.id }); } });
  const run = useQuery({ queryKey: ['pipeline-run', runId], queryFn: ({ signal }) => api.pipelineOperation(runId!, signal).then(asRun), enabled: Boolean(runId), refetchInterval: query => ['queued', 'running'].includes(query.state.data?.state ?? '') ? 1000 : false });
  const secondId = run.data?.state === 'awaiting-review' ? run.data.next_plan_id : undefined;
  const second = useQuery({ queryKey: ['pipeline-plan', secondId], queryFn: ({ signal }) => api.pipelineGetPlan(secondId!, signal).then(asPlan), enabled: Boolean(secondId) });
  const approveSecond = useMutation({ mutationFn: (value: Plan) => api.pipelineApproveSecond(runId!, { plan_id: value.id, approved_digest: value.digest, idempotency_key: requestKey('pipeline-second', `${runId}:${value.digest}`) }).then(asRun), onSuccess: () => { setConfirmedSecond(false); void run.refetch(); void client.invalidateQueries({ queryKey: ['pipeline-operations'] }); } });
  const history = useQuery({ queryKey: ['pipeline-operations'], queryFn: ({ signal }) => api.pipelineOperations(signal).then(value => value as unknown as { items: Run[] }), refetchInterval: 5000 });
  const current = plan.data;
  const expired = current ? Date.parse(current.expires_at) <= Date.now() : false;
  return <>
    <div className="heading"><p className="eyebrow">Piceli deploy</p><h1>Pipeline</h1><p className="subtitle">Plan the operator-configured Pipeline, review its stages, then approve the exact current hash.</p></div>
    <div className="run-actions"><button className="primary" disabled={prepared.isPending || admit.isPending} onClick={() => { setParams({}); setConfirmed(false); prepared.mutate(); }}>{prepared.isPending ? 'Planning…' : 'Prepare new plan'}</button>{(planId || runId) && <Link to="/pipeline">View run history</Link>}</div>
    {prepared.isError && <Failure error={prepared.error} retry={() => prepared.mutate()} />}
    {plan.isPending && planId && !runId && <Loading text="Loading Pipeline plan…" />}
    {plan.isError && <Failure error={plan.error} retry={() => void plan.refetch()} />}
    {current && !runId && <section className="panel review-panel"><div className="panelhead"><div><p className="eyebrow">Exact combined plan</p><h2>{current.target.name} / {current.target.namespace}</h2></div><Badge value={current.materialized ? 'planned' : 'pending'} /></div><div className="panelbody">
      <dl className="facts"><dt>Approval hash</dt><dd><code>{current.digest}</code></dd><dt>Expires</dt><dd>{new Date(current.expires_at).toLocaleString()}</dd></dl>
      <Stages stages={current.stages} />
      {!current.materialized && <Notice title="First approval: build and deliver">Only the listed preliminary stages will run now. After delivery, review and approve a second exact hash before rollout.</Notice>}
      {expired && <Notice title="Plan expired">Prepare a new plan to review current source and cluster state.</Notice>}
      {!expired && <label className="confirmation"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> I reviewed the listed stages, target and exact combined hash.</label>}
      {admit.isError && <Failure error={admit.error} retry={() => admit.mutate(current)} />}
    </div><div className="review-footer"><p className="small muted">The service replans under the runner lock and refuses a changed hash before executing.</p><button className="primary" disabled={expired || !confirmed || admit.isPending} onClick={() => admit.mutate(current)}>{admit.isPending ? 'Submitting…' : current.materialized ? 'Approve and deploy' : 'Approve build and delivery'}</button></div></section>}
    {runId && <>{run.isPending && <Loading text="Loading Pipeline run…" />}{run.isError && <Failure error={run.error} retry={() => void run.refetch()} />}{run.data && <section className="panel control-card"><div className="panelhead"><h2>Pipeline run</h2><Badge value={run.data.state} /></div><div className="panelbody"><p className="small muted"><code>{run.data.id}</code>{run.data.runner_id ? ` · journal ${run.data.runner_id}` : ''}</p>{run.data.error_code && <Notice title="Run did not complete" danger>{run.data.error_code}. Inspect the Piceli deploy journal before preparing a new plan.</Notice>}<ol className="pipeline-stages">{Object.entries(run.data.stages).map(([name, state]) => <li key={name}><strong>{label(name)}</strong> <Badge value={state} /></li>)}</ol></div></section>}{second.isPending && secondId && <Loading text="Loading materialized plan…" />}{second.isError && <Failure error={second.error} retry={() => void second.refetch()} />}{second.data && secondId && <section className="panel review-panel"><div className="panelhead"><div><p className="eyebrow">Second approval · materialized plan</p><h2>{second.data.target.name} / {second.data.target.namespace}</h2></div><Badge value="awaiting-review" /></div><div className="panelbody"><dl className="facts"><dt>Approval hash</dt><dd><code>{second.data.digest}</code></dd><dt>Expires</dt><dd>{new Date(second.data.expires_at).toLocaleString()}</dd></dl><Stages stages={second.data.stages} />{Date.parse(second.data.expires_at) <= Date.now() && <Notice title="Plan expired">Prepare a new plan.</Notice>}{Date.parse(second.data.expires_at) > Date.now() && <label className="confirmation"><input type="checkbox" checked={confirmedSecond} onChange={event => setConfirmedSecond(event.target.checked)} /> I reviewed the materialized plan and exact hash.</label>}{approveSecond.isError && <Failure error={approveSecond.error} retry={() => approveSecond.mutate(second.data!)} />}</div><div className="review-footer"><p className="small muted">Rollout starts only after this approval passes a fresh plan check.</p><button className="primary" disabled={!confirmedSecond || Date.parse(second.data.expires_at) <= Date.now() || approveSecond.isPending} onClick={() => approveSecond.mutate(second.data!)}>Approve rollout</button></div></section>}</>}
    <section className="panel control-card"><div className="panelhead"><h2>Recent Pipeline runs</h2><button onClick={() => void history.refetch()} disabled={history.isFetching}>Refresh</button></div><div className="panelbody">{history.isError && <Failure error={history.error} retry={() => void history.refetch()} />}{history.data?.items.length ? history.data.items.map(item => <p className="pipeline-history" key={item.id}><Link to={`/pipeline?run=${encodeURIComponent(item.id)}`}>{new Date(item.created_at).toLocaleString()}</Link><Badge value={item.state} /></p>) : <p className="small muted">No browser Pipeline run has been admitted yet.</p>}</div></section>
  </>;
}
