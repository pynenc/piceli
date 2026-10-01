import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api } from '../../api/client';
import { Failure, formatTime, Loading, Notice } from '../../components/State';
import type { Environment } from './Composition';

type Action = { allowed: boolean; reason: string | null };
type Ref = { source: string; branch: string; commit: string };
type Options = {
  env: string; promote: Action; approve: Action; wake: Action;
  refs: Ref[]; plan_hash: string | null; stopped_since: string | null;
  summary: { namespace: string | null; revision: Record<string, string>; components: string[] };
};
type Intent = 'promote' | 'approve' | 'wake';

const explain = (reason: string | null) => ({
  'gitops-promote-not-allowed': 'This environment does not allow promotion.',
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
  const [confirmed, setConfirmed] = useState(false);
  const review = useRef<HTMLDivElement>(null);
  useEffect(() => { if (intent) review.current?.focus(); }, [intent]);
  const mutate = useMutation({
    mutationFn: async () => {
      const options = query.data;
      if (!options || !intent) throw new Error('Refresh the environment before continuing.');
      if (intent === 'approve' && options.plan_hash) return api.approveNamedEnvironment(environment.name, { plan_hash: options.plan_hash });
      if (intent === 'wake') return api.wakeNamedEnvironment(environment.name);
      const choice = (options.refs ?? []).find(item => `${item.source}/${item.branch}@${item.commit}` === selected);
      if (intent === 'promote' && choice) return api.promoteNamedEnvironment(environment.name, { branch: choice.branch, commit: choice.commit });
      throw new Error('Select a published branch and commit before continuing.');
    },
    onSuccess: () => {
      setIntent(null); setConfirmed(false);
      void client.invalidateQueries({ queryKey: ['composition'] });
    },
  });
  const choose = (next: Intent) => { setIntent(next); setConfirmed(false); mutate.reset(); };
  const options = query.data;
  const refs = Array.isArray(options?.refs) ? options.refs : [];
  const disabled = (action?: Action) => !canChange || !action?.allowed || query.isPending;
  const why = (action?: Action) => !canChange ? explain('not-authorized') : explain(action?.reason ?? null);
  const selectedRef = refs.find(item => `${item.source}/${item.branch}@${item.commit}` === selected);
  return <div className="named-env-actions" aria-label="Environment actions">
    {query.isPending && <Loading text="Loading actions…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {options?.stopped_since && <span className="idle-stop-note">Idle-stopped since {formatTime(options.stopped_since)}</span>}
    <div className="run-actions">
      <button disabled={disabled(options?.promote)} title={disabled(options?.promote) ? why(options?.promote) : undefined} onClick={() => choose('promote')}>Promote</button>
      <button disabled={disabled(options?.approve)} title={disabled(options?.approve) ? why(options?.approve) : undefined} onClick={() => choose('approve')}>Approve</button>
      {environment.state === 'stopped' && <button disabled={disabled(options?.wake)} title={disabled(options?.wake) ? why(options?.wake) : undefined} onClick={() => choose('wake')}>Wake</button>}
    </div>
    {options && !options.promote?.allowed && <span className="sr-only">Promote unavailable: {why(options.promote)}</span>}
    {intent && options && <div className="control-review named-env-review" role="region" aria-label={`${intent} review`} ref={review} tabIndex={-1}>
      <h2>{intent === 'promote' ? 'Review promotion' : intent === 'approve' ? 'Review pending plan' : 'Wake idle environment'}</h2>
      <dl className="facts"><dt>Environment</dt><dd>{environment.name}</dd><dt>Namespace</dt><dd>{options.summary.namespace ?? 'Not created yet'}</dd>
        {intent === 'approve' && <><dt>Exact plan hash</dt><dd><code>{options.plan_hash}</code></dd><dt>Revision</dt><dd>{Object.entries(options.summary.revision).map(([source, sha]) => <span key={source}>{source} @ <code>{sha}</code><br /></span>)}</dd><dt>Components</dt><dd>{options.summary.components.join(', ') || 'None reported'}</dd></>}
        {intent === 'wake' && <><dt>Stopped since</dt><dd>{formatTime(options.stopped_since)}</dd><dt>Action</dt><dd>Request a sync; the controller will start this environment again.</dd></>}
      </dl>
      {intent === 'promote' && <label>Published branch and commit<select value={selected} onChange={event => { setSelected(event.target.value); setConfirmed(false); }}><option value="">Select a ref</option>{refs.map(ref => <option key={`${ref.source}/${ref.branch}@${ref.commit}`} value={`${ref.source}/${ref.branch}@${ref.commit}`}>{ref.source} · {ref.branch}@{ref.commit.slice(0, 12)}</option>)}</select></label>}
      {selectedRef && intent === 'promote' && <p className="small">This requests {selectedRef.branch}@<code>{selectedRef.commit}</code> for {environment.name}. The controller may still require a separate plan approval.</p>}
      <label className="confirmation"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> I reviewed this exact {intent === 'approve' ? 'plan hash' : intent === 'promote' ? 'branch and commit' : 'wake request'}.</label>
      <div className="run-actions"><button className="primary" disabled={!confirmed || mutate.isPending || (intent === 'promote' && !selectedRef)} onClick={() => mutate.mutate()}>Confirm {intent}</button><button onClick={() => { setIntent(null); setConfirmed(false); }}>Cancel</button></div>
      {mutate.isError && <Notice title="Request not completed" danger>{mutate.error instanceof ApiError ? explain(mutate.error.detail.code) : 'The connection ended before the controller accepted the request. Refresh the status before retrying.'}</Notice>}
    </div>}
    {mutate.isSuccess && <Notice title="Request recorded">The controller will process it on its next poll. Refresh to see the new state.</Notice>}
  </div>;
}
