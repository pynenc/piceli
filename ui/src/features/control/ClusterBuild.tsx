import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../../api/client';
import { Badge, Failure, Loading, Notice } from '../../components/State';
import { requestKey } from '../delivery/requests';

type Plan = { id: string; digest: string; commit: string; cache_key: string; expires_at: string; preview: { namespace: string; job: string; cache_claim: string; node_facts: Record<string, unknown> } };
type Run = { id: string; state: string; actor: string; approved_digest: string; created_at: string; images?: Record<string, unknown>; error_code?: string };

export function ClusterBuild() {
  const [params, setParams] = useSearchParams();
  const [commit, setCommit] = useState('');
  const [branch, setBranch] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const planId = params.get('plan');
  const runId = params.get('run');
  const planned = useMutation({ mutationFn: () => api.clusterBuildPlan({ commit, cache_key: branch }).then(value => value as Plan), onSuccess: plan => { setConfirmed(false); setParams({ plan: plan.id }); } });
  const plan = useQuery({ queryKey: ['cluster-build-plan', planId], queryFn: ({ signal }) => api.clusterBuildGetPlan(planId!, signal).then(value => value as Plan), enabled: Boolean(planId) && !runId });
  const admitted = useMutation({ mutationFn: (value: Plan) => api.clusterBuildAdmit({ plan_id: value.id, approved_digest: value.digest, idempotency_key: requestKey('cluster-build', `${value.id}:${value.digest}`) }).then(value => value as Run), onSuccess: run => setParams({ run: run.id }) });
  const run = useQuery({ queryKey: ['cluster-build-run', runId], queryFn: ({ signal }) => api.clusterBuildOperation(runId!, signal).then(value => value as Run), enabled: Boolean(runId), refetchInterval: query => ['queued', 'running'].includes(query.state.data?.state ?? '') ? 1000 : false });
  const history = useQuery({ queryKey: ['cluster-build-history'], queryFn: ({ signal }) => api.clusterBuildOperations(signal).then(value => value as { items: Run[] }), refetchInterval: 5000 });
  const current = plan.data;
  return <>
    <div className="heading"><p className="eyebrow">Installed builder</p><h1>Cluster build</h1><p className="subtitle">Build an exact Git commit in a separate Job using the operator-configured repository, Secret and registry.</p></div>
    <section className="panel control-card"><div className="panelbody"><label>Full Git commit<input value={commit} onChange={event => setCommit(event.target.value.trim())} placeholder="40 or 64 lowercase hex characters" /></label><label>Branch cache key<input value={branch} onChange={event => setBranch(event.target.value)} placeholder="main" /></label><button className="primary" disabled={!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(commit) || !branch || planned.isPending} onClick={() => planned.mutate()}>{planned.isPending ? 'Planning…' : 'Prepare build plan'}</button>{planned.isError && <Failure error={planned.error} retry={() => planned.mutate()} />}</div></section>
    {plan.isPending && planId && !runId && <Loading text="Loading build plan…" />}{plan.isError && <Failure error={plan.error} retry={() => void plan.refetch()} />}
    {current && !runId && <section className="panel review-panel"><div className="panelhead"><h2>Review cluster build</h2><Badge value="planned" /></div><div className="panelbody"><dl className="facts"><dt>Namespace</dt><dd>{current.preview.namespace}</dd><dt>Commit</dt><dd><code>{current.commit}</code></dd><dt>Job</dt><dd>{current.preview.job}</dd><dt>Cache claim</dt><dd>{current.preview.cache_claim}</dd><dt>Approval hash</dt><dd><code>{current.digest}</code></dd><dt>Expires</dt><dd>{new Date(current.expires_at).toLocaleString()}</dd></dl><p className="small muted">The Job receives Git credentials from its named Secret. The UI holds no credential value.</p>{Date.parse(current.expires_at) <= Date.now() ? <Notice title="Plan expired">Prepare a new build plan.</Notice> : <label className="confirmation"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> I reviewed the commit, target and exact build hash.</label>}{admitted.isError && <Failure error={admitted.error} retry={() => admitted.mutate(current)} />}</div><div className="review-footer"><button className="primary" disabled={!confirmed || Date.parse(current.expires_at) <= Date.now() || admitted.isPending} onClick={() => admitted.mutate(current)}>Approve cluster build</button></div></section>}
    {runId && <>{run.isPending && <Loading text="Loading build run…" />}{run.isError && <Failure error={run.error} retry={() => void run.refetch()} />}{run.data && <section className="panel control-card"><div className="panelhead"><h2>Build run</h2><Badge value={run.data.state} /></div><div className="panelbody"><p><code>{run.data.approved_digest}</code></p>{run.data.error_code && <Notice title="Build did not complete" danger>{run.data.error_code}. Prepare a new plan after inspecting the Job and state.</Notice>}{run.data.images && <pre>{JSON.stringify(run.data.images, null, 2)}</pre>}</div></section>}<Link to="/cluster-build">Review another build</Link></>}
    <section className="panel control-card"><div className="panelhead"><h2>Recent builds</h2><button onClick={() => void history.refetch()} disabled={history.isFetching}>Refresh</button></div><div className="panelbody">{history.isError && <Failure error={history.error} retry={() => void history.refetch()} />}{history.data?.items.length ? history.data.items.map(item => <p className="pipeline-history" key={item.id}><Link to={`/cluster-build?run=${encodeURIComponent(item.id)}`}>{new Date(item.created_at).toLocaleString()}</Link><Badge value={item.state} /></p>) : <p className="small muted">No approved build runs yet.</p>}</div></section>
  </>;
}
