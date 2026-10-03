import { useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { applicationPath } from '../../api/client';
import type { Operation } from '../../api/generated';
import { Badge, formatTime } from '../../components/State';
import './operation-history.css';

const needsAttention = (operation: Operation) => ['failed', 'interrupted'].includes(operation.state) || operation.deployment_outcome === 'failed' || operation.checks_outcome === 'failed';

export function OperationTimeline({ operations }: { operations: Operation[] }) {
  const [params, setParams] = useSearchParams();
  const attention = params.get('activity') === 'needs-attention';
  const query = params.get('activitySearch') ?? '';
  const search = query.trim().toLowerCase();
  const oldest = params.get('activitySort') === 'oldest';
  const ordered = useMemo(() => [...operations].sort((a, b) => ((Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0)) * (oldest ? -1 : 1)), [operations, oldest]);
  const attentionCount = operations.filter(needsAttention).length;
  const shown = ordered.filter(operation => (!attention || needsAttention(operation)) && `${operation.id} ${operation.engine_release ?? ''} ${operation.actor} ${operation.plan_id} ${operation.error_code ?? ''}`.toLowerCase().includes(search));
  function update(key: string, value: string) {
    setParams(previous => { const next = new URLSearchParams(previous); if (value) next.set(key, value); else next.delete(key); return next; }, { replace: true });
  }
  return <section className="activity-workbench compact-run-history" aria-label="Deployment activity">
    <div className="activity-tools"><div className="activity-filters" role="group" aria-label="Run status filter"><button aria-pressed={!attention} onClick={() => update('activity', '')}>All runs <strong>{operations.length}</strong></button><button aria-pressed={attention} onClick={() => update('activity', 'needs-attention')}>Needs attention <strong>{attentionCount}</strong></button></div><label className="run-history-search"><span>Search runs</span><input type="search" value={query} onChange={event => update('activitySearch', event.target.value)} placeholder="Release, run, actor or plan…" /></label><label className="run-history-sort">Sort runs<select value={oldest ? 'oldest' : 'newest'} onChange={event => update('activitySort', event.target.value === 'oldest' ? 'oldest' : '')}><option value="newest">Newest first</option><option value="oldest">Oldest first</option></select></label></div>
    <p className="small muted activity-scope" aria-live="polite">{shown.length} of {operations.length} recorded operations · {oldest ? 'Oldest first' : 'Newest first'}{attention ? ' · Failed or interrupted runs, or failed deployment/check outcomes' : ''}</p>
    {operations.length === 0 ? <div className="panel empty"><h3>No recorded operations</h3><p>Operations submitted to this service will appear here.</p></div> : shown.length === 0 ? <div className="panel empty"><h3>No runs match these filters</h3><p>Change the search or select All runs to see more recorded activity.</p></div> : <ol className="activity-timeline run-register">{shown.map(operation => {
      const path = applicationPath(operation.application_id);
      const runPath = `/runs/${encodeURIComponent(operation.id)}`;
      const stage = operation.stages?.find(entry => ['failed', 'interrupted', 'running'].includes(entry.state));
      return <li key={operation.id} data-attention={needsAttention(operation)}><article className="activity-run operation-row panel" aria-label={`Run ${operation.id}`}>
        <div className="run-record-main"><div className="run-record-identity"><h3><Link to={runPath}>{operation.engine_release ?? operation.id}</Link></h3><p><code title={operation.id}>{operation.id}</code> · Attempt {operation.attempt ?? 1}</p></div><div className="run-record-time"><time dateTime={operation.created_at}>{formatTime(operation.created_at)}</time><span>{operation.trigger.toUpperCase()} · {operation.actor}</span></div><div className="run-record-state"><Badge value={operation.state} />{operation.checks_outcome === 'failed' && <span className="run-check-failure">Checks failed</span>}</div><div className="run-record-links"><Link to={runPath}>Open run →</Link><Link className="run-logs-link" to={`${runPath}?runView=logs#execution-journal`}>Open logs</Link>{operation.plan_id && <Link to={`${path}/changes?plan=${encodeURIComponent(operation.plan_id)}`}>Review plan</Link>}</div></div>
        {(stage || operation.error_code) && <div className="run-record-signal">{stage && <p><strong>{stage.name}</strong><span>{stage.state}{stage.reason ? ` · ${stage.reason}` : ''}</span></p>}{operation.error_code && <p className="activity-error">Recorded reason <code>{operation.error_code}</code></p>}</div>}
        <div className="run-record-evidence">{operation.stages?.length ? <details className="activity-stage-details"><summary>{operation.stages.length} execution {operation.stages.length === 1 ? 'stage' : 'stages'}</summary><ol>{operation.stages.map((entry, index) => <li key={`${entry.name}/${index}`}><span>{entry.name}</span><Badge value={entry.state} />{entry.reason && <p>{entry.reason}</p>}</li>)}</ol></details> : <span className="run-no-stages">No stages recorded</span>}<details className="run-record-outcomes"><summary>Outcomes and identity</summary><div className="activity-outcomes"><span>Deployment <Badge value={operation.deployment_outcome ?? 'unknown'} /></span><span>Checks <Badge value={operation.checks_outcome ?? 'unknown'} /></span><span>Updated {formatTime(operation.updated_at)}</span></div><dl><dt>Approved plan</dt><dd><code>{operation.approved_digest}</code></dd><dt>Plan ID</dt><dd><code>{operation.plan_id || 'Not recorded'}</code></dd></dl></details><div className="run-record-related">{operation.plan_id && <Link to={`${path}/activity?history=revisions&compareTo=${encodeURIComponent(operation.plan_id)}`}>Compare revision</Link>}<Link to={`${path}/resources`}>Inspect resources</Link>{operation.recovery_of && <Link to={`/runs/${encodeURIComponent(operation.recovery_of)}`}>Recovery of {operation.recovery_of}</Link>}</div></div>
      </article></li>;
    })}</ol>}
  </section>;
}
