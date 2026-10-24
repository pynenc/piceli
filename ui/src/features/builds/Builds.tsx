import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import { Badge, Failure, formatTime, Loading, Notice } from '../../components/State';
import { seconds } from '../trains/Trains';
import '../control/operational-harbor.css';

type Stage = { stage: string; started_at?: string | null; finished_at?: string | null; seconds?: number | null };
export type BuildRecord = {
  id: string; kind: 'nix-publish' | 'images' | 'train-handoff'; target: string | null;
  trigger: { kind: string | null; by: string | null; detail: string | null };
  commit: string | null; tag: string | null; branch: string | null; host: string | null;
  stage: string; stages: Stage[]; log_tail: string | null; requested_at: string | null; started_at: string | null; finished_at: string | null;
  seconds: number | null; plan_digest: string | null; plan: Record<string, unknown> | null; reason: string | null; images: string[];
  running_on: string[]; actions: { approve: boolean; retry: boolean; cancel: boolean };
};
export type BuildsPage = { configured: boolean; schema: string; builds: BuildRecord[]; stages: string[]; targets?: string[] };

const KINDS: Record<string, string> = { 'nix-publish': 'NixRelease publish', images: 'Image build', 'train-handoff': 'Train handoff' };
const short = (value?: string | null) => value ? value.slice(0, 12) : '—';
const DONE = new Set(['published', 'failed', 'cancelled', 'rejected']);

function Review({ build, onDone }: { build: BuildRecord; onDone: () => void }) {
  const [confirmed, setConfirmed] = useState(false);
  const client = useQueryClient();
  const approve = useMutation({
    mutationFn: () => api.approveBuild(String(build.target), { plan_hash: String(build.plan_digest) }),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ['builds'] }); onDone(); },
  });
  const plan = build.plan ?? {};
  return <section className="panel control-card" aria-label={`Review plan of ${build.target}`}>
    <div className="panelhead"><h2>Review pending plan</h2></div>
    <div className="panelbody">
      <dl>
        <dt>Exact plan digest</dt><dd><code>{build.plan_digest}</code></dd>
        <dt>Commit</dt><dd><code>{build.commit}</code>{build.tag ? ` · tag ${build.tag}` : ''}{build.branch ? ` · branch ${build.branch}` : ''}</dd>
        <dt>Attribute</dt><dd><code>{String(plan.attribute ?? '—')}</code> on {String(plan.builder ?? build.host ?? '—')}</dd>
        <dt>Writes</dt><dd>{[String(plan.manifest ?? ''), ...((plan.pointers as string[] | undefined) ?? [])].filter(Boolean).map(key => <span key={key}><code>{key}</code><br /></span>)}</dd>
        {plan.pinned ? <><dt>Promotion gate</dt><dd><code>{JSON.stringify(plan.pinned)}</code></dd></> : null}
      </dl>
      {approve.isError && <Failure error={approve.error} />}
      <label className="confirmation"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> I reviewed this exact plan digest.</label>
      <div className="run-actions"><button className="primary" disabled={!confirmed || approve.isPending} onClick={() => approve.mutate()}>Confirm approve</button><button onClick={onDone}>Cancel</button></div>
    </div>
  </section>;
}

function BuildCard({ build, canChange }: { build: BuildRecord; canChange: boolean }) {
  const [reviewing, setReviewing] = useState(false);
  const client = useQueryClient();
  const act = useMutation({
    mutationFn: (action: 'retry' | 'cancel') => action === 'retry' ? api.retryBuild(String(build.target)) : api.cancelBuild(String(build.target)),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['builds'] }),
  });
  const live = !DONE.has(build.stage);
  const ref = build.tag ?? (build.branch ? `branch ${build.branch}` : null);
  return <section className="panel control-card" aria-label={`Build ${build.id}`}>
    <div className="panelhead"><h2>{build.target ?? '—'} <Badge value={build.stage} /></h2><span className="small muted">{KINDS[build.kind] ?? build.kind} · requested {formatTime(build.requested_at)}</span></div>
    <div className="panelbody">
      <ul className="control-workloads" aria-label="Build facts">
        <li><strong>Trigger</strong><span>{build.trigger.kind ?? '—'}{build.trigger.by ? ` by ${build.trigger.by}` : ''}{build.trigger.detail ? <span className="small muted"> ({build.trigger.detail})</span> : null}</span></li>
        <li><strong>Commit</strong><span><code>{short(build.commit)}</code>{ref ? ` · ${ref}` : ''}</span></li>
        <li><strong>Build host</strong><span>{build.host ?? (live ? 'not claimed yet' : '—')}</span></li>
        <li><strong>Duration</strong><span>{seconds(build.seconds)}{live ? ' so far' : ''}</span></li>
        <li><strong>Runs on</strong><span>{build.running_on.length ? build.running_on.join(', ') : 'no target yet'}</span></li>
        {build.images.length > 0 && <li><strong>Images</strong><span>{build.images.join(', ')}</span></li>}
        {build.reason && <li><strong>Reason</strong><span><code>{build.reason}</code></span></li>}
      </ul>
      {build.stages.length > 0 && <ol className="control-workloads" aria-label="Stages">{build.stages.map(item => <li key={`${item.stage}-${item.started_at}`}><strong>{item.stage}</strong><span>{item.finished_at ? seconds(item.seconds) : <Badge value="running" />} · {formatTime(item.started_at)}</span></li>)}</ol>}
      {build.log_tail && <details open={live || build.stage === 'failed'}><summary className="small">Log tail</summary><pre className="log-output" aria-label={`Log of ${build.target}`}>{build.log_tail}</pre></details>}
      {canChange && build.target && build.kind !== 'train-handoff' && <div className="run-actions">
        {build.actions.approve && build.plan_digest && <button onClick={() => setReviewing(true)}>Approve</button>}
        {build.actions.retry && <button disabled={act.isPending} onClick={() => act.mutate('retry')}>Retry</button>}
        {build.actions.cancel && <button disabled={act.isPending} onClick={() => act.mutate('cancel')}>Cancel build</button>}
      </div>}
      {act.isError && <Failure error={act.error} />}
      {act.isSuccess && <p className="small muted">Requested; the controller acts on its next poll.</p>}
      {reviewing && <Review build={build} onDone={() => setReviewing(false)} />}
    </div>
  </section>;
}

function BuildNow({ targets }: { targets: string[] }) {
  const [target, setTarget] = useState('');
  const [ref, setRef] = useState('');
  const client = useQueryClient();
  const start = useMutation({
    mutationFn: () => api.startBuild({ target, ref: ref.trim() || null }),
    onSuccess: () => { setRef(''); void client.invalidateQueries({ queryKey: ['builds'] }); },
  });
  return <section className="panel control-card" aria-label="Build now">
    <div className="panelhead"><h2>Build now</h2><span className="small muted">The same gate, plan and approval as an automatic build</span></div>
    <div className="panelbody">
      <form className="run-actions" onSubmit={event => { event.preventDefault(); if (target) start.mutate(); }}>
        <label>Target<select value={target} onChange={event => setTarget(event.target.value)}><option value="">Select a target</option>{targets.map(name => <option key={name} value={name}>{name}</option>)}</select></label>
        <label>Tag or commit<input value={ref} placeholder="newest" onChange={event => setRef(event.target.value)} /></label>
        <button className="primary" type="submit" disabled={!target || start.isPending}>Build now</button>
      </form>
      {start.isError && <Failure error={start.error} />}
      {start.isSuccess && <p className="small muted">Build of {target} requested; it appears here once the controller plans it.</p>}
    </div>
  </section>;
}

export function Builds({ canChange = false }: { canChange?: boolean }) {
  const [target, setTarget] = useState('');
  const [state, setState] = useState('');
  const query = useQuery({
    queryKey: ['builds', target, state],
    queryFn: ({ signal }) => api.builds({ ...(target ? { target } : {}), ...(state ? { state } : {}) }, signal).then(value => value as unknown as BuildsPage),
    refetchInterval: 3000,
  });
  const page = query.data;
  const builds = page?.builds ?? [];
  const targets = page?.targets ?? [];
  return <div className="cluster-overview">
    <div className="heading detail-heading"><div><p className="eyebrow">Delivery</p><h1>Builds</h1><p className="subtitle">Every build the GitOps controller knows: NixRelease publishes, image builds and train handoffs, with what triggered each one, its stages, log tail and the targets that run it now. The CLI shows the same list (<code>piceli gitops builds</code>).</p></div><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh</button></div>
    {query.isPending && <Loading text="Loading builds…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {page && !page.configured && <Notice title="No GitOps controller">Install one with <code>piceli gitops enable</code>; its builds show here.</Notice>}
    {page?.configured && <>
      <div className="statusbar" aria-label="Builds state">
        <div className="statusitem"><span className="statuslabel">Active</span><strong>{builds.filter(item => !DONE.has(item.stage)).length}</strong></div>
        <div className="statusitem"><span className="statuslabel">Waiting for approval</span><strong>{builds.filter(item => item.stage === 'approval-required').length}</strong></div>
        <div className="statusitem"><span className="statuslabel">Failed</span><strong>{builds.filter(item => item.stage === 'failed').length}</strong></div>
      </div>
      {canChange && <BuildNow targets={targets} />}
      <div className="run-actions" aria-label="Filters">
        <label>Target<select value={target} onChange={event => setTarget(event.target.value)}><option value="">Every target</option>{targets.map(name => <option key={name} value={name}>{name}</option>)}</select></label>
        <label>State<select value={state} onChange={event => setState(event.target.value)}><option value="">Every state</option><option value="active">active</option>{page.stages.map(name => <option key={name} value={name}>{name}</option>)}</select></label>
      </div>
      {builds.length === 0 && <p className="small muted">No build matches.</p>}
      {builds.map(build => <BuildCard key={build.id} build={build} canChange={canChange} />)}
    </>}
  </div>;
}
