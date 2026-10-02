import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../../api/client';
import { Badge, Failure, formatTime, Loading, Notice } from '../../components/State';
import { requestKey } from '../delivery/requests';
import { useExpired } from '../delivery/useExpired';
import '../delivery/delivery.css';

type Stage = { name: string; state?: string; action?: string; why?: string; summary?: Record<string, number>; changes?: Array<{ operation: string; kind: string; name: string }>; checks?: unknown[]; claims?: unknown[]; writers?: unknown[]; release?: string; rollback_on_failed_checks?: boolean };
type Plan = { id: string; digest: string; materialized: boolean; phase: 'preliminary' | 'final'; expires_at: string; target: { name: string; namespace: string }; stages: Stage[] };
type Run = { id: string; state: string; plan_id: string; next_plan_id?: string; stages: Record<string, string>; error_code?: string; runner_id?: string; created_at: string };
const asPlan = (value: unknown) => value as Plan;
const asRun = (value: unknown) => value as Run;
const label = (name: string) => name === 'prerollout' ? 'Pre-rollout checks' : name === 'backup' ? 'Restore point' : name[0].toUpperCase() + name.slice(1);

function Stages({ stages }: { stages: Stage[] }) {
  return <ol className="pipeline-stages delivery-stages">{stages.map((stage, index) => <li key={stage.name} data-state={stage.state ?? stage.action ?? 'planned'}><span className="delivery-stage-number" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span><div className="delivery-stage-content"><h3>{label(stage.name)} <Badge value={stage.state ?? stage.action ?? 'planned'} /></h3>{stage.why && <p className="small muted">{stage.why}</p>}{stage.summary && <p className="small muted">{Object.entries(stage.summary).map(([key, count]) => `${count} ${key}`).join(' · ')}</p>}{stage.changes?.length ? <ul>{stage.changes.map((change, index) => <li key={`${change.kind}/${change.name}/${index}`}>{change.operation} {change.kind}/{change.name}</li>)}</ul> : null}{stage.checks?.length ? <details><summary>{stage.checks.length} declared checks</summary><pre>{JSON.stringify(stage.checks, null, 2)}</pre></details> : null}{stage.claims?.length ? <details><summary>{stage.claims.length} claims in restore point</summary><pre>{JSON.stringify(stage.claims, null, 2)}</pre></details> : null}{stage.writers?.length ? <details><summary>Writers will be quiesced before backup.</summary><pre>{JSON.stringify(stage.writers, null, 2)}</pre></details> : null}{stage.release && <p className="small muted">Release <code>{stage.release}</code></p>}{stage.rollback_on_failed_checks !== undefined && <p className="small muted">Rollback on failed checks: {stage.rollback_on_failed_checks ? 'enabled' : 'disabled'}</p>}</div></li>)}</ol>;
}

function PlanGate({ plan, second = false, pending, error, approve }: { plan: Plan; second?: boolean; pending: boolean; error: Error | null; approve: () => void }) {
  const [confirmed, setConfirmed] = useState(false);
  const expired = useExpired(plan.expires_at);
  return <section className="panel review-panel delivery-review">
    <div className="panelhead"><div><p className="eyebrow">{second ? 'Second approval · materialized plan' : 'Exact combined plan'}</p><h2>{plan.target.name} / {plan.target.namespace}</h2></div><Badge value={second ? 'awaiting-review' : plan.materialized ? 'planned' : 'pending'} /></div>
    <div className="delivery-review-grid"><div className="delivery-evidence"><div className="delivery-section-heading"><div><p className="eyebrow">Release path</p><h3>Stages in this plan</h3></div><span className="small muted">{plan.stages.length} stages</span></div><Stages stages={plan.stages} />{plan.stages.length === 0 && <p className="small muted">No stages were supplied for this plan.</p>}</div>
      <aside className="delivery-decision" aria-label={second ? 'Materialized plan approval' : 'Pipeline plan approval'}><p className="eyebrow">{second ? '02 / Rollout decision' : plan.materialized ? 'Rollout decision' : '01 / Build decision'}</p><h3>{second || plan.materialized ? 'Review before rollout' : 'Build, then review again'}</h3>
        <dl className="facts"><dt>Target</dt><dd>{plan.target.name} / {plan.target.namespace}</dd><dt>Plan</dt><dd><code>{plan.id}</code></dd><dt>Phase</dt><dd>{plan.phase}</dd><dt>Expires</dt><dd>{formatTime(plan.expires_at)}</dd></dl><div className="delivery-digest"><span>Approval hash</span><code>{plan.digest}</code></div>
        {!plan.materialized && <Notice title="First approval: build and deliver">Only the listed preliminary stages will run now. After delivery, review and approve a second exact hash before rollout.</Notice>}
        {expired && <Notice title="Plan expired">Prepare a new plan to review current source and cluster state.</Notice>}
        {!expired && <label className="confirmation"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> {second ? 'I reviewed the materialized plan and exact hash.' : 'I reviewed the listed stages, target and exact combined hash.'}</label>}
        {error && <Failure error={error} />}
        <div className="review-footer"><p className="small muted">{second ? 'Rollout starts only after this approval passes a fresh plan check.' : 'The service replans under the runner lock and refuses a changed hash before executing.'}</p><button className="primary" disabled={expired || !confirmed || pending} onClick={approve}>{pending ? 'Submitting…' : second ? 'Approve rollout' : plan.materialized ? 'Approve and deploy' : 'Approve build and delivery'}</button></div>
      </aside>
    </div>
  </section>;
}

export function Pipeline() {
  const [params, setParams] = useSearchParams();
  const planId = params.get('plan');
  const runId = params.get('run');
  const client = useQueryClient();
  const prepared = useMutation({ mutationFn: () => api.pipelinePlan().then(asPlan), onSuccess: plan => setParams({ plan: plan.id }) });
  const plan = useQuery({ queryKey: ['pipeline-plan', planId], queryFn: ({ signal }) => api.pipelineGetPlan(planId!, signal).then(asPlan), enabled: Boolean(planId) && !runId });
  const admit = useMutation({ mutationFn: (value: Plan) => api.pipelineAdmit({ plan_id: value.id, approved_digest: value.digest, idempotency_key: requestKey('pipeline', `${value.id}:${value.digest}`) }).then(asRun), onSuccess: run => { void client.invalidateQueries({ queryKey: ['pipeline-operations'] }); setParams({ run: run.id }); } });
  const run = useQuery({ queryKey: ['pipeline-run', runId], queryFn: ({ signal }) => api.pipelineOperation(runId!, signal).then(asRun), enabled: Boolean(runId), refetchInterval: query => ['queued', 'running'].includes(query.state.data?.state ?? '') ? 1000 : false });
  const secondId = run.data?.state === 'awaiting-review' ? run.data.next_plan_id : undefined;
  const second = useQuery({ queryKey: ['pipeline-plan', secondId], queryFn: ({ signal }) => api.pipelineGetPlan(secondId!, signal).then(asPlan), enabled: Boolean(secondId) });
  const approveSecond = useMutation({ mutationFn: (value: Plan) => api.pipelineApproveSecond(runId!, { plan_id: value.id, approved_digest: value.digest, idempotency_key: requestKey('pipeline-second', `${runId}:${value.digest}`) }).then(asRun), onSuccess: () => { void run.refetch(); void client.invalidateQueries({ queryKey: ['pipeline-operations'] }); } });
  const history = useQuery({ queryKey: ['pipeline-operations'], queryFn: ({ signal }) => api.pipelineOperations(signal).then(value => value as unknown as { items: Run[] }), refetchInterval: 5000 });
  const current = plan.data;
  return <div className="delivery-workspace">
    <div className="heading detail-heading"><div><p className="eyebrow">Delivery / Pipeline</p><h1>Pipeline</h1><p className="subtitle">Review the release path, then approve its exact plan.</p></div><div className="run-actions"><button className="primary" disabled={prepared.isPending || admit.isPending} onClick={() => { setParams({}); prepared.mutate(); }}>{prepared.isPending ? 'Planning…' : 'Prepare new plan'}</button>{(planId || runId) && <Link to="/pipeline">View run history</Link>}</div></div>
    {prepared.isError && <Failure error={prepared.error} retry={() => prepared.mutate()} />}
    {plan.isPending && planId && !runId && <Loading text="Loading Pipeline plan…" />}{plan.isError && <Failure error={plan.error} retry={() => void plan.refetch()} />}
    {current && !runId && <PlanGate key={`${current.id}:${current.digest}`} plan={current} pending={admit.isPending} error={admit.error} approve={() => admit.mutate(current)} />}
    {runId && <>{run.isPending && <Loading text="Loading Pipeline run…" />}{run.isError && <Failure error={run.error} retry={() => void run.refetch()} />}{run.data && <section className="panel control-card"><div className="panelhead"><div><p className="eyebrow">Recorded execution</p><h2>Pipeline run</h2></div><Badge value={run.data.state} /></div><div className="panelbody"><p className="small muted"><code>{run.data.id}</code>{run.data.runner_id ? ` · journal ${run.data.runner_id}` : ''}</p>{run.data.error_code && <Notice title="Run did not complete" danger>{run.data.error_code}. Inspect the Piceli deploy journal before preparing a new plan.</Notice>}<ol className="delivery-progress" aria-label="Recorded pipeline stages">{Object.entries(run.data.stages).map(([name, state], index) => <li key={name} data-state={state}><span className="delivery-stage-number" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span><strong>{label(name)}</strong><Badge value={state} /></li>)}</ol>{Object.keys(run.data.stages).length === 0 && <p className="small muted">No stage evidence has been recorded yet.</p>}</div></section>}
      {second.isPending && secondId && <Loading text="Loading materialized plan…" />}{second.isError && <Failure error={second.error} retry={() => void second.refetch()} />}{second.data && secondId && <PlanGate key={`${runId}:${second.data.id}:${second.data.digest}:${approveSecond.isSuccess}`} second plan={second.data} pending={approveSecond.isPending} error={approveSecond.error} approve={() => approveSecond.mutate(second.data!)} />}
    </>}
    {!planId && !runId && <section className="panel delivery-intro"><p className="eyebrow">An explicit decision at each boundary</p><h2>Start with the configured pipeline.</h2><p>Prepare a plan to see its target, stages and exact approval hash. A preliminary build requires a separate review of the materialized plan before rollout.</p></section>}
    <section className="panel control-card delivery-history"><div className="panelhead"><h2>Recent Pipeline runs</h2><button onClick={() => void history.refetch()} disabled={history.isFetching}>Refresh</button></div><div className="panelbody">{history.isError && <Failure error={history.error} retry={() => void history.refetch()} />}{history.data?.items.length ? history.data.items.map(item => <div className="pipeline-history" key={item.id}><div><Link to={`/pipeline?run=${encodeURIComponent(item.id)}`}>{new Date(item.created_at).toLocaleString()}</Link><p className="small muted"><code>{item.id}</code></p></div><Badge value={item.state} /></div>) : <p className="small muted">No browser Pipeline run has been admitted yet.</p>}</div></section>
  </div>;
}
