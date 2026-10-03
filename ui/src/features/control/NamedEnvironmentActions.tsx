import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api } from '../../api/client';
import { Failure, formatTime, Loading, Notice } from '../../components/State';
import type { Environment, PendingPlan } from './Composition';
import './environment-history.css';
import { countEntries } from './EnvironmentHistory';

type Action = { allowed: boolean; reason: string | null };
type Ref = { source: string; branch: string; commit: string };
type Options = {
  env: string; promote: Action; approve: Action; wake: Action; stop?: Action; start?: Action;
  refs: Ref[]; plan_hash: string | null; stopped_since: string | null;
  pending_plan?: PendingPlan | null;
  summary: { namespace: string | null; revision: Record<string, string>; components: string[] };
};
type Intent = 'promote' | 'approve' | 'wake' | 'stop' | 'start';

const explain = (reason: string | null) => ({
  'gitops-promote-not-allowed': 'This environment does not allow promotion.',
  'gitops-env-stop-declared': 'The composition declares this environment stopped (Environment(stopped=True)); remove the declaration to start it.',
  'ui-plan-stale': 'There is no current plan hash awaiting approval. Refresh the environment.',
  'ui-operation-unavailable': 'Wake is available only after the idle-stop policy has stopped this environment.',
  'ui-sync-unavailable': 'The controller request inbox is unavailable. Retry after its connection returns.',
  'ui-controller-absent': 'The controller has not published status yet.',
  'ui-sync-target-unknown': 'The environment is no longer in controller status. Refresh the page.',
  'ui-observation-unavailable': 'The controller has not published promotion policy and a usable branch head for this environment.',
  'not-authorized': 'Your session cannot change this environment.',
} as Record<string, string>)[reason ?? ''] ?? 'This action is unavailable in the current environment state.';

export function NamedEnvironmentActions({ environment, canChange }: { environment: Environment; canChange: boolean }) {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['composition', 'actions', environment.name], queryFn: ({ signal }) => api.namedEnvironmentActions(environment.name, signal).then(value => value as Options), refetchInterval: 10000 });
  const [intent, setIntent] = useState<Intent | null>(null);
  const [selected, setSelected] = useState('');
  const [confirmedIdentity, setConfirmedIdentity] = useState<string | null>(null);
  const review = useRef<HTMLDivElement>(null);
  useEffect(() => { if (intent) review.current?.focus(); }, [intent]);
  const options = query.data;
  const refs = Array.isArray(options?.refs) ? options.refs : [];
  const selectedRef = refs.find(item => `${item.source}/${item.branch}@${item.commit}` === selected);
  const identity = options && intent ? JSON.stringify([
    environment.name, options.env, intent, options.summary.namespace,
    intent === 'approve' ? [options.plan_hash, options.pending_plan?.combined_hash ?? null, (options.pending_plan?.changes ?? []).map(item => `${item.operation} ${item.kind}/${item.name}`), Object.entries(options.summary.revision).sort(([a], [b]) => a.localeCompare(b)), [...options.summary.components].sort()] : null,
    intent === 'promote' && selectedRef ? [selectedRef.source, selectedRef.branch, selectedRef.commit] : null,
    intent === 'wake' ? [environment.state, options.stopped_since] : null,
    intent === 'stop' || intent === 'start' ? [environment.state, environment.reason ?? null] : null,
  ]) : null;
  const confirmed = identity !== null && confirmedIdentity === identity;
  const ownerStopped = environment.state === 'stopped' && (environment.reason === 'declared' || environment.reason === 'requested');
  const disabled = (action?: Action) => !canChange || !action?.allowed || query.isPending || query.isError || options?.env !== environment.name;
  const unavailable = !intent || disabled(options?.[intent]) || (intent === 'approve' && !options?.plan_hash) || (intent === 'promote' && !selectedRef) || (intent === 'wake' && environment.state !== 'stopped') || (intent === 'start' && environment.state !== 'stopped');
  const why = (action?: Action) => !canChange ? explain('not-authorized') : explain(action?.reason ?? null);
  const mutate = useMutation({
    mutationFn: async () => {
      if (!options || !intent || !confirmed || unavailable) throw new Error('Review the current environment and action before continuing.');
      if (intent === 'approve' && options.plan_hash) return api.approveNamedEnvironment(environment.name, { plan_hash: options.plan_hash });
      if (intent === 'wake') return api.wakeNamedEnvironment(environment.name);
      if (intent === 'stop') return api.stopNamedEnvironment(environment.name);
      if (intent === 'start') return api.startNamedEnvironment(environment.name);
      if (intent === 'promote' && selectedRef) return api.promoteNamedEnvironment(environment.name, { branch: selectedRef.branch, commit: selectedRef.commit });
      throw new Error('Select a published branch and commit before continuing.');
    },
    onSuccess: () => {
      setIntent(null); setConfirmedIdentity(null);
      void client.invalidateQueries({ queryKey: ['composition'] });
    },
  });
  const choose = (next: Intent) => { setIntent(next); setConfirmedIdentity(null); mutate.reset(); };
  return <div className="named-env-actions" aria-label="Environment actions">
    {query.isPending && <Loading text="Loading actions…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {options?.stopped_since && <span className="idle-stop-note">Idle-stopped since {formatTime(options.stopped_since)}</span>}
    <div className="run-actions">
      <button disabled={disabled(options?.promote)} title={disabled(options?.promote) ? why(options?.promote) : undefined} onClick={() => choose('promote')}>Promote</button>
      <button disabled={disabled(options?.approve)} title={disabled(options?.approve) ? why(options?.approve) : undefined} onClick={() => choose('approve')}>Approve</button>
      {environment.state === 'stopped' && !ownerStopped && <button disabled={disabled(options?.wake)} title={disabled(options?.wake) ? why(options?.wake) : undefined} onClick={() => choose('wake')}>Wake</button>}
      {options?.start && ownerStopped && <button disabled={disabled(options.start)} title={disabled(options.start) ? why(options.start) : undefined} onClick={() => choose('start')}>Start</button>}
      {options?.stop && environment.state !== 'stopped' && <button disabled={disabled(options.stop)} title={disabled(options.stop) ? why(options.stop) : undefined} onClick={() => choose('stop')}>Stop</button>}
    </div>
    {options && !options.promote?.allowed && <span className="sr-only">Promote unavailable: {why(options.promote)}</span>}
    {intent && options && <div className="control-review named-env-review" role="region" aria-label={`${intent} review`} ref={review} tabIndex={-1}>
      <h2>{intent === 'promote' ? 'Review promotion' : intent === 'approve' ? 'Review pending plan' : intent === 'stop' ? 'Stop environment' : intent === 'start' ? 'Start environment' : 'Wake idle environment'}</h2>
      <dl className="facts"><dt>Environment</dt><dd>{environment.name}</dd><dt>Namespace</dt><dd>{options.summary.namespace ?? 'Not created yet'}</dd>
        {intent === 'approve' && <><dt>Exact plan hash</dt><dd><code>{options.plan_hash ?? 'No current plan'}</code></dd><dt>Revision</dt><dd>{Object.entries(options.summary.revision).map(([source, sha]) => <span key={source}>{source} @ <code>{sha}</code><br /></span>)}</dd><dt>Components</dt><dd>{options.summary.components.join(', ') || 'None reported'}</dd>
          <dt>Changes</dt><dd>{options.pending_plan ? <PlanChanges plan={options.pending_plan} /> : 'The controller did not publish this plan’s changes (a controller older than 0.14.6). Review it with piceli gitops status.'}</dd></>}
        {intent === 'stop' && <><dt>Action</dt><dd>Request a stop: the controller scales every workload to zero, keeps its volumes and neither plans nor deploys it until it is started.</dd></>}
        {intent === 'start' && <><dt>Action</dt><dd>Request a start: the controller scales the workloads back and deploys the current revision when it moved (with the usual approval).</dd></>}
        {intent === 'wake' && <><dt>Stopped since</dt><dd>{formatTime(options.stopped_since)}</dd><dt>Action</dt><dd>Request a sync; the controller will start this environment again.</dd></>}
      </dl>
      {intent === 'promote' && <label>Published branch and commit<select value={selected} onChange={event => { setSelected(event.target.value); setConfirmedIdentity(null); }}><option value="">Select a ref</option>{refs.map(ref => <option key={`${ref.source}/${ref.branch}@${ref.commit}`} value={`${ref.source}/${ref.branch}@${ref.commit}`}>{ref.source} · {ref.branch}@{ref.commit.slice(0, 12)}</option>)}</select></label>}
      {selectedRef && intent === 'promote' && <p className="small">This requests {selectedRef.branch}@<code>{selectedRef.commit}</code> for {environment.name}. The controller may still require a separate plan approval.</p>}
      {disabled(options[intent]) && <Notice title="Action unavailable">{options.env !== environment.name ? 'This action evidence belongs to another environment. Refresh before continuing.' : why(options[intent])}</Notice>}
      <label className="confirmation"><input type="checkbox" checked={confirmed} disabled={Boolean(unavailable)} onChange={event => setConfirmedIdentity(event.target.checked ? identity : null)} /> I reviewed this exact {intent === 'approve' ? 'plan hash' : intent === 'promote' ? 'branch and commit' : `${intent} request`}.</label>
      <div className="run-actions"><button className="primary" disabled={!confirmed || mutate.isPending || Boolean(unavailable)} onClick={() => mutate.mutate()}>Confirm {intent}</button><button onClick={() => { setIntent(null); setConfirmedIdentity(null); }}>Cancel</button></div>
      {mutate.isError && <Notice title="Request not completed" danger>{mutate.error instanceof ApiError ? explain(mutate.error.detail.code) : 'The connection ended before the controller accepted the request. Refresh the status before retrying.'}</Notice>}
    </div>}
    {mutate.isSuccess && <Notice title="Request recorded">The controller will process it on its next poll. Refresh to see the new state.</Notice>}
  </div>;
}

/** What the pending plan changes: counts and objects, before the exact hash is approved. */
function PlanChanges({ plan }: { plan: PendingPlan }) {
  const counts = countEntries(plan.counts).map(([key, count]) => `${count} ${key}`).join(' · ');
  return <div className="pending-plan" aria-label="Pending plan changes">
    {plan.create_namespace && <p className="small">Creates the environment’s namespace and every object of its rendered model.</p>}
    {counts && <p className="small muted">{counts}</p>}
    {plan.changes.length ? <ul className="pending-plan-changes">{plan.changes.map((item, index) => <li key={index}><span className="history-op">{item.operation}</span>{item.kind}/<strong>{item.name}</strong></li>)}</ul> : !plan.create_namespace && <p className="small">No object changes; only the plan’s identity (images, checks or policy) changed.</p>}
    {plan.changes_total > plan.changes.length && <p className="small muted">{plan.changes_total - plan.changes.length} more not listed.</p>}
    {plan.stop.length > 0 && <p className="small">Stops {plan.stop.join(', ')} to stay within the environment budget.</p>}
    {plan.combined_hash && <p className="small muted">Deploy plan <code>{plan.combined_hash}</code></p>}
  </div>;
}
