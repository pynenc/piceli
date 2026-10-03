import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api } from '../../api/client';
import { Badge, Failure, formatTime, Loading, Notice } from '../../components/State';
import './environment-history.css';

export type HistoryComponent = { name: string; source?: string | null; commit?: string | null; digest?: string | null; state: string; built: boolean };
export type HistoryCheck = { name: string; type?: string | null; passed?: boolean | null; code?: string | null; detail?: string | null; duration?: number | null };
export type HistoryRun = {
  id: string; run_id?: string | null; started_at?: string | null; finished_at?: string | null;
  state: string; action?: string | null; reason?: string | null; trigger?: string | null;
  sources: { name: string; commit?: string | null; ref?: string | null }[];
  plan_hash?: string | null; combined_hash?: string | null;
  approved_by?: { via: string; at?: string | null } | null; release?: string | null;
  components: HistoryComponent[]; built: string[]; rolled: string[]; unchanged: string[];
  plan?: { counts: Record<string, number>; changes: { operation: string; kind: string; name: string }[]; changes_total: number } | null;
  checks?: { passed?: boolean | null; verification: boolean; results: HistoryCheck[] } | null;
  verification?: { state: string; trigger?: string | null; checks_hash?: string | null } | null;
  failure?: { stage?: string | null; reason?: string | null; message?: string | null; failed_checks: { name?: string | null; code?: string | null }[]; log_tail?: string | null; kept_job?: string | null } | null;
  stages: { name: string; state: string; seconds?: number | null }[]; seconds?: number | null;
  recorded_by?: string | null;
};
export type History = { configured: boolean; available: boolean; generated_at?: string | null; truncated: boolean; environments: { name: string; runs: HistoryRun[] }[] };

/** A history document; anything else (an older server) reads as unpublished. */
const asHistory = (value: unknown): History => {
  const found = value as Partial<History> | null;
  return found && Array.isArray(found.environments) ? found as History : { configured: Boolean(found?.configured), available: false, truncated: false, environments: [] };
};
const short = (value?: string | null, size = 12) => (value ? value.replace(/^sha256:/, '').slice(0, size) : 'unknown');
const seconds = (value?: number | null) => (typeof value === 'number' ? (value >= 60 ? `${Math.floor(value / 60)}m ${Math.round(value % 60)}s` : `${value < 10 ? value.toFixed(2) : value.toFixed(1)}s`) : '');
const approver: Record<string, string> = { cli: 'piceli gitops approve (CLI)', ui: 'Piceli UI', policy: "the owner's approval policy", request: 'an approval request' };

/** Object counts with ``no-op`` last. */
export const countEntries = (counts: Record<string, number>) => Object.entries(counts).sort(([a], [b]) => Number(a === 'no-op') - Number(b === 'no-op') || a.localeCompare(b));

/** A run's outcome in words: what it did, not only its state. */
export function outcome(run: HistoryRun): string {
  if (run.state === 'degraded') return 'Checks failed; the release keeps running';
  if (run.state === 'deployed' && run.action === 'verified') return 'Verified (checks changed); rolled nothing';
  if (run.state === 'deployed' && run.action === 'unchanged') return 'Nothing to change';
  if (run.state === 'deployed') return run.rolled.length ? `Deployed; rolled ${run.rolled.join(', ')}` : 'Deployed; rolled nothing';
  if (run.state === 'approval-required') return 'Waiting for approval of its plan';
  if (run.state === 'retrying') return `Failed (${run.reason ?? 'error'}); retrying`;
  if (run.state === 'failed') return `Failed (${run.reason ?? run.failure?.reason ?? 'error'})`;
  return run.state;
}

function RunDetails({ run }: { run: HistoryRun }) {
  const failing = run.checks?.results.filter(item => item.passed === false) ?? [];
  return <div className="history-run-body">
    <dl className="facts history-run-facts">
      <dt>Trigger</dt><dd>{run.trigger ?? 'Not recorded'}</dd>
      <dt>Approved by</dt><dd>{run.approved_by ? <>{approver[run.approved_by.via] ?? run.approved_by.via}{run.approved_by.at ? ` · ${formatTime(run.approved_by.at)}` : ''}</> : 'Not recorded'}</dd>
      <dt>Plan hash</dt><dd>{run.plan_hash ? <code title={run.plan_hash}>{run.plan_hash}</code> : 'Not recorded'}</dd>
      {run.combined_hash && <><dt>Deploy plan</dt><dd><code title={run.combined_hash}>{run.combined_hash}</code></dd></>}
      {run.release && <><dt>Release</dt><dd><code>{run.release}</code></dd></>}
      {run.run_id && <><dt>Run</dt><dd><code>{run.run_id}</code></dd></>}
    </dl>
    <section aria-label="Run sources and commits"><h4>Sources</h4>{run.sources.length ? <ul className="history-sources">{run.sources.map(source => <li key={source.name}><strong>{source.name}</strong><span>{source.ref?.replace(/^refs\/(heads|tags)\//, '') ?? 'ref not recorded'}</span><code title={source.commit ?? undefined}>{short(source.commit, 12)}</code></li>)}</ul> : <p className="small muted">No source revision recorded.</p>}</section>
    {run.components.length > 0 && <section aria-label="Run images"><h4>Components</h4><ul className="history-components">{run.components.map(item => <li key={item.name}><strong>{item.name}</strong><code title={item.digest ?? undefined}>{item.digest ? `sha256:${short(item.digest)}` : 'no image'}</code><span className="history-component-tags">{item.built && <span className="history-tag built">built</span>}{run.rolled.includes(item.name) ? <span className="history-tag rolled">rolled</span> : run.unchanged.includes(item.name) ? <span className="history-tag">unchanged</span> : item.state === 'failed' ? <span className="history-tag failed">failed</span> : null}</span></li>)}</ul></section>}
    {run.plan && <section aria-label="Run plan"><h4>Plan · {run.plan.changes_total} change{run.plan.changes_total === 1 ? '' : 's'}</h4><p className="small muted">{countEntries(run.plan.counts).map(([key, count]) => `${count} ${key}`).join(' · ') || 'No object counts recorded'}</p>{run.plan.changes.length > 0 && <ul className="history-changes">{run.plan.changes.map((item, index) => <li key={index}><span className="history-op">{item.operation}</span>{item.kind}/<strong>{item.name}</strong></li>)}</ul>}{run.plan.changes_total > run.plan.changes.length && <p className="small muted">{run.plan.changes_total - run.plan.changes.length} more not listed.</p>}</section>}
    {run.checks && <section aria-label="Run checks"><h4>Checks {run.checks.verification ? '(verification)' : ''} · {run.checks.passed === true ? 'passed' : run.checks.passed === false ? 'failed' : 'unknown'}</h4><ul className="history-checks">{run.checks.results.map((item, index) => <li key={index} className={item.passed === false ? 'failed' : undefined}><Badge value={item.passed === false ? 'failed' : item.passed ? 'succeeded' : 'unknown'} /><strong>{item.name}</strong>{item.type && <span className="small muted">{item.type}</span>}<span className="small">{seconds(item.duration)}</span>{item.detail && <span className="small muted history-detail">{item.detail}</span>}{item.code && item.passed === false && <code className="small">{item.code}</code>}</li>)}</ul></section>}
    {run.failure && <section aria-label="Run failure"><Notice title={`Failure${run.failure.stage ? ` in ${run.failure.stage}` : ''}`} danger>
      {run.failure.reason && <p className="small">Code <code>{run.failure.reason}</code> · <code>piceli explain {run.failure.reason}</code></p>}
      {run.failure.message && <p className="small">{run.failure.message}</p>}
      {(run.failure.failed_checks.length > 0 || failing.length > 0) && <p className="small">Failing checks: {(run.failure.failed_checks.length ? run.failure.failed_checks.map(item => item.name) : failing.map(item => item.name)).join(', ')}</p>}
      {run.failure.kept_job && <p className="small">Build Job kept for diagnosis: <code>{run.failure.kept_job}</code></p>}
      {run.failure.log_tail && <pre className="history-tail" aria-label="Failure log tail">{run.failure.log_tail}</pre>}
    </Notice></section>}
    {run.stages.length > 0 && <section aria-label="Run stages"><h4>Stages{run.seconds ? ` · ${seconds(run.seconds)}` : ''}</h4><ul className="history-stages">{run.stages.map(stage => <li key={stage.name}><span>{stage.name}</span><Badge value={stage.state === 'done' ? 'succeeded' : stage.state} /><span className="small muted">{seconds(stage.seconds)}</span></li>)}</ul></section>}
    {run.recorded_by === 'run' && <p className="small muted">Recorded only by the deploy run journal (before the controller recorded its trigger and approver).</p>}
  </div>;
}

/** One environment's runs, newest first; the newest is open. */
export function RunList({ env, runs }: { env: string; runs: HistoryRun[] }) {
  if (!runs.length) return <p className="small muted history-empty">No deploy of {env} is recorded yet.</p>;
  return <ol className="history-runs" aria-label={`Runs of ${env}`}>{runs.map((run, index) => <li key={run.id}><details open={index === 0} className="history-run" aria-label={`Run ${run.run_id ?? run.id}`}>
    <summary><Badge value={run.state} /><span className="history-run-outcome">{outcome(run)}</span><span className="history-run-meta"><time dateTime={run.started_at ?? undefined}>{formatTime(run.started_at)}</time>{run.trigger && <span> · {run.trigger}</span>}</span></summary>
    <RunDetails run={run} />
  </details></li>)}</ol>;
}

export function EnvironmentHistory({ env }: { env: string }) {
  const query = useQuery({ queryKey: ['composition', 'history', env], queryFn: ({ signal }) => api.compositionEnvironmentHistory(env, signal).then(asHistory), refetchInterval: 15000 });
  const runs = query.data?.environments.find(item => item.name === env)?.runs ?? [];
  return <section className="panel control-card environment-history" aria-label="Deployment history">
    <div className="panelhead"><h2>Deployment history <span className="count">{runs.length}</span></h2><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh history</button></div>
    <div className="panelbody">
      {query.isPending && <Loading text="Loading deployment history…" />}
      {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
      {query.data && !query.data.available && <Notice title="No run history published">The controller has not published its run history yet. Controllers from Piceli 0.14.6 publish it after their next poll.</Notice>}
      {query.data?.available && <RunList env={env} runs={runs} />}
      {query.data?.truncated && <p className="small muted">Older runs were left out to keep the history small.</p>}
    </div>
  </section>;
}

/** Deployment history of every environment (Delivery → Deployment history). */
export function CompositionHistory({ selected, onSelect }: { selected: string | null; onSelect: (env: string) => void }) {
  const query = useQuery({ queryKey: ['composition', 'history'], queryFn: ({ signal }) => api.compositionHistory(signal).then(asHistory), refetchInterval: 15000 });
  const environments = query.data?.environments ?? [];
  const current = environments.find(item => item.name === selected) ?? environments[0];
  return <div className="composition-history">
    {query.isPending && <Loading text="Loading environment runs…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {query.data && !query.data.configured && <Notice title="No GitOps controller status yet">The history appears after the controller’s first poll.</Notice>}
    {query.data?.configured && !query.data.available && <Notice title="No run history published">The controller has not published its run history yet. Controllers from Piceli 0.14.6 publish it after their next poll.</Notice>}
    {query.data?.available && <>
      <div className="history-scope"><label><span>Environment</span><select value={current?.name ?? ''} onChange={event => onSelect(event.target.value)}>{environments.map(item => <option key={item.name} value={item.name}>{item.name} · {item.runs.length} run{item.runs.length === 1 ? '' : 's'}</option>)}</select></label>{current && <Link to={`/composition/environments/${encodeURIComponent(current.name)}`}>Open environment <span aria-hidden="true">↗</span></Link>}<span className="small muted">Published {formatTime(query.data.generated_at)}</span></div>
      {current ? <RunList env={current.name} runs={current.runs} /> : <Notice title="No environments yet">The controller has not resolved an environment.</Notice>}
    </>}
  </div>;
}
