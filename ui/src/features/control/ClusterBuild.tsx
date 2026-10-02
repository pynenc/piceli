import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../../api/client';
import { Badge, Failure, formatTime, Loading, Notice } from '../../components/State';
import { requestKey } from '../delivery/requests';
import { useExpired } from '../delivery/useExpired';
import '../delivery/delivery.css';

type Plan = { id: string; digest: string; commit: string; cache_key: string; expires_at: string; preview: { namespace: string; job: string; cache_claim: string; node_facts: Record<string, unknown> } };
type Run = { id: string; state: string; actor: string; approved_digest: string; created_at: string; images?: Record<string, unknown>; error_code?: string };

function BuildReview({ plan, pending, error, approve }: { plan: Plan; pending: boolean; error: Error | null; approve: () => void }) {
  const [confirmed, setConfirmed] = useState(false);
  const expired = useExpired(plan.expires_at);
  return <section className="panel review-panel delivery-review"><div className="panelhead"><div><p className="eyebrow">Exact build plan</p><h2>Review cluster build</h2></div><Badge value="planned" /></div>
    <div className="delivery-review-grid"><div className="delivery-evidence"><div className="delivery-section-heading"><div><p className="eyebrow">Execution context</p><h3>Source, job and cache</h3></div></div>
      <dl className="build-context"><div><dt><span aria-hidden="true">01</span> Source commit</dt><dd><code>{plan.commit}</code></dd></div><div><dt><span aria-hidden="true">02</span> Builder Job</dt><dd><strong>{plan.preview.job}</strong><span className="small muted">Namespace · {plan.preview.namespace}</span></dd></div><div><dt><span aria-hidden="true">03</span> Cache volume</dt><dd><strong>{plan.preview.cache_claim}</strong><span className="small muted">Branch key · {plan.cache_key}</span></dd></div></dl>
      <p className="small muted">The Job receives Git credentials from its named Secret. The UI holds no credential value.</p><details><summary>Node facts used by this plan</summary><pre>{JSON.stringify(plan.preview.node_facts, null, 2)}</pre></details>
    </div><aside className="delivery-decision" aria-label="Cluster build approval"><p className="eyebrow">Approval boundary</p><h3>Run this exact build</h3><dl className="facts"><dt>Namespace</dt><dd>{plan.preview.namespace}</dd><dt>Plan</dt><dd><code>{plan.id}</code></dd><dt>Expires</dt><dd>{formatTime(plan.expires_at)}</dd></dl><div className="delivery-digest"><span>Approval hash</span><code>{plan.digest}</code></div>
      {expired ? <Notice title="Plan expired">Prepare a new build plan.</Notice> : <label className="confirmation"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> I reviewed the commit, target and exact build hash.</label>}{error && <Failure error={error} />}
      <div className="review-footer"><p className="small muted">Approval authorizes this build Job. Deploying its output requires a separate deployment review.</p><button className="primary" disabled={!confirmed || expired || pending} onClick={approve}>Approve cluster build</button></div>
    </aside></div>
  </section>;
}

export function ClusterBuild() {
  const [params, setParams] = useSearchParams();
  const [commit, setCommit] = useState('');
  const [branch, setBranch] = useState('');
  const planId = params.get('plan');
  const runId = params.get('run');
  const planned = useMutation({ mutationFn: () => api.clusterBuildPlan({ commit, cache_key: branch }).then(value => value as Plan), onSuccess: plan => setParams({ plan: plan.id }) });
  const plan = useQuery({ queryKey: ['cluster-build-plan', planId], queryFn: ({ signal }) => api.clusterBuildGetPlan(planId!, signal).then(value => value as Plan), enabled: Boolean(planId) && !runId });
  const admitted = useMutation({ mutationFn: (value: Plan) => api.clusterBuildAdmit({ plan_id: value.id, approved_digest: value.digest, idempotency_key: requestKey('cluster-build', `${value.id}:${value.digest}`) }).then(value => value as Run), onSuccess: run => setParams({ run: run.id }) });
  const run = useQuery({ queryKey: ['cluster-build-run', runId], queryFn: ({ signal }) => api.clusterBuildOperation(runId!, signal).then(value => value as Run), enabled: Boolean(runId), refetchInterval: query => ['queued', 'running'].includes(query.state.data?.state ?? '') ? 1000 : false });
  const history = useQuery({ queryKey: ['cluster-build-history'], queryFn: ({ signal }) => api.clusterBuildOperations(signal).then(value => value as { items: Run[] }), refetchInterval: 5000 });
  const current = plan.data;
  return <div className="delivery-workspace">
    <div className="heading"><p className="eyebrow">Delivery / Installed builder</p><h1>Cluster build</h1><p className="subtitle">Turn an exact Git commit into a reviewed build Job.</p></div>
    <section className="panel control-card"><div className="panelhead"><div><p className="eyebrow">Build input</p><h2>Pin the source</h2></div><span className="small muted">Uses the operator-configured repository and registry</span></div><div className="panelbody"><div className="build-form"><label>Full Git commit<input value={commit} onChange={event => setCommit(event.target.value.trim())} placeholder="40 or 64 lowercase hex characters" spellCheck={false} autoComplete="off" /></label><label>Branch cache key<input value={branch} onChange={event => setBranch(event.target.value)} placeholder="main" spellCheck={false} /></label><button className="primary" disabled={!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(commit) || !branch || planned.isPending} onClick={() => planned.mutate()}>{planned.isPending ? 'Planning…' : 'Prepare build plan'}</button></div>{planned.isError && <Failure error={planned.error} retry={() => planned.mutate()} />}</div></section>
    {plan.isPending && planId && !runId && <Loading text="Loading build plan…" />}{plan.isError && <Failure error={plan.error} retry={() => void plan.refetch()} />}
    {current && !runId && <BuildReview key={`${current.id}:${current.digest}`} plan={current} pending={admitted.isPending} error={admitted.error} approve={() => admitted.mutate(current)} />}
    {runId && <>{run.isPending && <Loading text="Loading build run…" />}{run.isError && <Failure error={run.error} retry={() => void run.refetch()} />}{run.data && <section className="panel control-card"><div className="panelhead"><div><p className="eyebrow">Recorded execution</p><h2>Build run</h2></div><Badge value={run.data.state} /></div><div className="panelbody"><dl className="facts"><dt>Run</dt><dd><code>{run.data.id}</code></dd><dt>Actor</dt><dd>{run.data.actor}</dd><dt>Started</dt><dd>{formatTime(run.data.created_at)}</dd></dl><div className="delivery-digest"><span>Approved digest</span><code>{run.data.approved_digest}</code></div>{run.data.error_code && <Notice title="Build did not complete" danger>{run.data.error_code}. Prepare a new plan after inspecting the Job and state.</Notice>}{run.data.images && <><h3>Build outputs</h3><pre>{JSON.stringify(run.data.images, null, 2)}</pre></>}</div></section>}<div className="run-actions"><Link to="/cluster-build">Review another build</Link></div></>}
    <section className="panel control-card delivery-history"><div className="panelhead"><h2>Recent builds</h2><button onClick={() => void history.refetch()} disabled={history.isFetching}>Refresh</button></div><div className="panelbody">{history.isError && <Failure error={history.error} retry={() => void history.refetch()} />}{history.data?.items.length ? history.data.items.map(item => <div className="pipeline-history" key={item.id}><div><Link to={`/cluster-build?run=${encodeURIComponent(item.id)}`}>{new Date(item.created_at).toLocaleString()}</Link><p className="small muted"><code>{item.id}</code> · {item.actor}</p></div><Badge value={item.state} /></div>) : <p className="small muted">No approved build runs yet.</p>}</div></section>
  </div>;
}
