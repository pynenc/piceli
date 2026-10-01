import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api } from '../../api/client';
import { Badge, Failure, Loading, Notice } from '../../components/State';

type Workload = { workload: string; ready: number; replicas: number };
type Environment = {
  branch: string; namespace: string; main: boolean; state: string; health: string;
  commit?: string | null; age_seconds?: number | null; build?: string | null;
  deploy?: string | null; workloads: Workload[];
  application_id?: string;
  gitops?: { state?: string; plan_hash?: string; commit?: string } | null;
};
type EnvironmentPage = { configured: boolean; items: Environment[] };
type ActionResult = {
  state: string; branch: string; namespace: string; env_hash?: string;
  create_namespace?: boolean; stop?: string[]; delete?: { claims: string[]; volumes: string[] };
  source?: { source: string; namespace: string; point: string }; claims?: string[];
};
type ControllerEntry = {
  branch: string; namespace?: string; commit?: string; deployed_commit?: string;
  state?: string; health?: string; plan_hash?: string;
};
type ControllerView = {
  configured: boolean; controller?: { state?: string; last_poll?: string; poll_seconds?: number } | null;
  envs: ControllerEntry[];
};

const asEnvironments = (value: unknown) => value as EnvironmentPage;
const asController = (value: unknown) => value as ControllerView;
const asAction = (value: unknown) => value as ActionResult;

export function Environments({ canChange }: { canChange: boolean }) {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ['environments'], queryFn: ({ signal }) => api.environments(signal).then(asEnvironments) });
  const [branch, setBranch] = useState('');
  const [verb, setVerb] = useState<'up' | 'down' | 'seed'>('up');
  const [source, setSource] = useState('');
  const [review, setReview] = useState<ActionResult | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const action = useMutation({
    mutationFn: (approved_hash?: string) => api.environmentAction({ verb, branch, approved_hash: approved_hash ?? null, source: verb === 'seed' ? source : null }).then(asAction),
    onSuccess: result => {
      if (result.state === 'approval-required') { setReview(result); setConfirmed(false); }
      else { setReview(null); void queryClient.invalidateQueries({ queryKey: ['environments'] }); }
    },
  });
  const select = (value: string) => { setBranch(value); setReview(null); action.reset(); setConfirmed(false); };
  const selectVerb = (value: 'up' | 'down' | 'seed') => { setVerb(value); setReview(null); action.reset(); setConfirmed(false); };
  return <>
    <div className="heading"><p className="eyebrow">Per-branch deployment</p><h1>Environments</h1><p className="subtitle">Namespaces and workload health from the configured Pipeline.</p></div>
    {query.isPending && <Loading text="Loading environments…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {query.data && !query.data.configured && <Notice title="No environment pipeline">Start the local UI with a trusted Pipeline that declares EnvConfig.</Notice>}
    {query.data?.items.map(env => <section className="panel control-card" key={`${env.branch}/${env.namespace}`}>
      <div className="panelhead"><div><h2>{env.branch}</h2><p className="small muted">{env.namespace || 'Namespace pending'}{env.main ? ' · main' : ''}</p></div><Badge value={env.health} /></div>
      <div className="panelbody"><dl className="facts"><dt>State</dt><dd>{env.gitops?.state ?? env.state}</dd><dt>Commit</dt><dd><code>{env.commit ?? env.gitops?.commit ?? 'Unknown'}</code></dd><dt>Age</dt><dd>{env.age_seconds == null ? 'Unknown' : `${Math.round(env.age_seconds / 60)} min`}</dd><dt>Build / deploy</dt><dd>{env.build ?? 'Unknown'} / {env.deploy ?? 'Unknown'}</dd></dl>
        {env.gitops?.state === 'approval-required' && <Notice title="Approval pending">Review the exact pending hash on the GitOps page.</Notice>}
        {env.application_id && <p className="small"><Link to={`/applications/${encodeURIComponent(env.application_id)}/resources`}>Inspect resources, current and previous logs, and access forwards</Link></p>}
        <h3>Workloads</h3>{env.workloads.length ? <ul className="control-workloads">{env.workloads.map(item => <li key={item.workload}><strong>{item.workload}</strong><span>{item.ready}/{item.replicas} ready</span></li>)}</ul> : <p className="small muted">No workload status available.</p>}
      </div>
    </section>)}
    {canChange && query.data?.configured && <section className="panel control-card"><div className="panelhead"><h2>Change an environment</h2></div><div className="panelbody">
      <p className="small muted">Piceli plans the requested change first. Approval applies only to the exact current plan hash.</p>
      <div className="intent-form"><label>Branch<input value={branch} onChange={event => select(event.target.value)} list="environment-branches" placeholder="wp-feature" /></label><datalist id="environment-branches">{query.data.items.map(env => <option key={env.branch} value={env.branch} />)}</datalist>
        <label>Action<select value={verb} onChange={event => selectVerb(event.target.value as 'up' | 'down' | 'seed')}><option value="up">Up</option><option value="down">Down</option><option value="seed">Seed from configured source</option></select></label>
        {verb === 'seed' && <label>Seed from environment<input value={source} onChange={event => { setSource(event.target.value); setReview(null); setConfirmed(false); }} list="environment-branches" placeholder="main" /></label>}
        <button disabled={!branch || (verb === 'seed' && !source) || action.isPending} onClick={() => action.mutate(undefined)}>Prepare plan</button></div>
      {action.isError && <Failure error={action.error} retry={() => action.mutate(undefined)} />}
      {action.data && action.data.state !== 'approval-required' && <Notice title="Environment request complete">{action.data.state} · {action.data.namespace}</Notice>}
      {review && <div className="control-review" role="region" aria-label="Environment plan review"><h3>Review {verb} of {review.branch}</h3><dl className="facts"><dt>Namespace</dt><dd>{review.namespace}</dd><dt>Plan hash</dt><dd><code>{review.env_hash}</code></dd>{review.delete && <><dt>Deletes</dt><dd>{review.delete.claims.length} claims, {review.delete.volumes.length} volumes</dd></>}{review.source && <><dt>Seed source</dt><dd>{review.source.source} / {review.source.point}</dd></>}</dl>
        {review.stop?.length ? <Notice title="Environment budget">This change stops {review.stop.join(', ')}.</Notice> : null}
        <label className="confirmation"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> I reviewed this exact environment plan.</label>
        <button className="primary" disabled={!confirmed || !review.env_hash || action.isPending} onClick={() => action.mutate(review.env_hash)}>Approve exact plan</button></div>}
    </div></section>}
    <p className="small muted">For a branch without a registered scope, use <code>piceli logs</code> and <code>piceli access</code> with an explicit environment.</p>
  </>;
}

export function GitOps({ canChange }: { canChange: boolean }) {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ['gitops'], queryFn: ({ signal }) => api.gitops(signal).then(asController), refetchInterval: 10000 });
  const [confirmed, setConfirmed] = useState('');
  const approve = useMutation({ mutationFn: (item: ControllerEntry) => api.approveGitops({ branch: item.branch, plan_hash: item.plan_hash! }), onSuccess: () => { setConfirmed(''); void queryClient.invalidateQueries({ queryKey: ['gitops'] }); } });
  const promote = useMutation({ mutationFn: (item: ControllerEntry) => api.promoteGitops({ branch: item.branch, commit: item.deployed_commit ?? item.commit! }), onSuccess: () => { setConfirmed(''); void queryClient.invalidateQueries({ queryKey: ['gitops'] }); } });
  return <><div className="heading"><p className="eyebrow">Controller</p><h1>GitOps</h1><p className="subtitle">Piceli’s installed controller reconciles Git. This page shows its status and submits reviewed requests.</p></div>
    {query.isPending && <Loading text="Loading controller status…" />}{query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {query.data && !query.data.configured && <Notice title="Controller not found">No controller status is published in the configured namespace.</Notice>}
    {query.data?.configured && <section className="panel control-card"><div className="panelhead"><h2>Controller status</h2><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh</button></div><div className="panelbody"><dl className="facts"><dt>State</dt><dd>{query.data.controller?.state ?? 'Unknown'}</dd><dt>Last poll</dt><dd>{query.data.controller?.last_poll ?? 'Unknown'}</dd></dl></div></section>}
    {approve.isError && <Failure error={approve.error} retry={() => void query.refetch()} />}{promote.isError && <Failure error={promote.error} retry={() => void query.refetch()} />}
    {query.data?.envs.map(env => {
      const commit = env.deployed_commit ?? env.commit;
      const approval = env.state === 'approval-required' && Boolean(env.plan_hash);
      return <section className="panel control-card" key={env.branch}><div className="panelhead"><div><h2>{env.branch}</h2><p className="small muted">{env.namespace ?? 'Namespace pending'}</p></div><Badge value={env.state ?? 'unknown'} /></div><div className="panelbody"><dl className="facts"><dt>Commit</dt><dd><code>{env.commit ?? 'Unknown'}</code></dd><dt>Deployed</dt><dd><code>{env.deployed_commit ?? 'Not deployed'}</code></dd>{approval && <><dt>Pending plan</dt><dd><code>{env.plan_hash}</code></dd></>}</dl>
        {canChange && (approval || commit) && <div className="control-review"><label className="confirmation"><input type="checkbox" checked={confirmed === env.branch} onChange={event => setConfirmed(event.target.checked ? env.branch : '')} /> I reviewed this branch, commit and pending plan.</label><div className="run-actions">{approval && <button className="primary" disabled={confirmed !== env.branch || approve.isPending} onClick={() => approve.mutate(env)}>Approve pending plan</button>}{commit && <button disabled={confirmed !== env.branch || promote.isPending} onClick={() => promote.mutate(env)}>Request promotion to main</button>}</div></div>}
      </div></section>;
    })}
    <p className="small muted"><Link to="/environments">View branch environments</Link>. A promotion is a controller request; main may still require a separate plan approval.</p>
  </>;
}
