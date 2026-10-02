import { useEffect, useRef, useState } from 'react';
import { useLocation, useSearchParams } from 'react-router-dom';
import type { Operation } from '../../api/generated';
import { label, Notice } from '../../components/State';
import { matchingRunJournal, resourceName } from './executionEvidence';
import './execution-evidence.css';

export function RunJournal({ operation }: { operation: Operation }) {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const panel = useRef<HTMLElement>(null);
  const view = params.get('runView') === 'logs' ? 'logs' : 'events';
  function setView(value: 'events' | 'logs') { setParams(previous => { const next = new URLSearchParams(previous); if (value === 'logs') next.set('runView', value); else next.delete('runView'); return next; }); }
  useEffect(() => { if (location.hash === '#execution-journal') { panel.current?.scrollIntoView?.({ block: 'start' }); panel.current?.focus({ preventScroll: true }); } }, [location.hash, operation.id]);
  const [capture, setCapture] = useState(0);
  const [search, setSearch] = useState('');
  const journal = matchingRunJournal(operation);
  const selectedLog = journal?.logs[capture] ?? journal?.logs[0];
  const actions = new Map(journal?.actions.map(action => [action.ordinal, action]));
  const events = [...(journal?.events ?? [])].sort((a, b) => a.sequence - b.sequence).filter(event => {
    const action = event.ordinal == null ? undefined : actions.get(event.ordinal);
    return `${event.state} ${event.sequence} ${action ? resourceName(action.resource) : ''}`.toLowerCase().includes(search.trim().toLowerCase());
  });
  return <section ref={panel} tabIndex={-1} id="execution-journal" className="panel run-journal" aria-label="Execution journal and logs"><div className="panelhead"><div><p className="eyebrow">Recorded evidence</p><h2>Journal and logs</h2></div>{journal && <span className="small muted">{journal.events.length} transitions · {journal.logs.length} captures</span>}</div>
    {!journal ? <div className="panelbody">{operation.journal ? <Notice title="Journal identity does not match this operation" danger>The returned journal cannot be connected to this engine execution.</Notice> : <p className="small muted">No resource execution journal is available for this operation.</p>}</div> : <>
      {journal.truncated && <div className="journal-notice"><Notice title="Partial journal evidence">This response is bounded. Some recorded events or captured output are not included.</Notice></div>}
      <div className="journal-toolbar"><div className="journal-view-controls" aria-label="Journal views"><button aria-pressed={view === 'events'} onClick={() => setView('events')}>Journal transitions <span>{journal.events.length}</span></button><button aria-pressed={view === 'logs'} onClick={() => setView('logs')}>Captured logs <span>{journal.logs.length}</span></button></div>{view === 'events' && <label>Find recorded transition<input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Resource, state or sequence" /></label>}</div>
      {view === 'events' ? <div className="journal-transitions"><p className="journal-context">Events retain the engine’s sequence numbers. <span>No event timestamps were recorded.</span></p>{events.length ? <ol aria-label="Recorded journal transitions">{events.map(event => { const action = event.ordinal == null ? undefined : actions.get(event.ordinal); return <li key={event.sequence} data-state={event.state}><span className="journal-sequence">#{event.sequence}</span><div><strong>{event.state}</strong><p>{action ? resourceName(action.resource) : event.ordinal == null ? 'Execution event' : `Action ${event.ordinal + 1} · resource identity unavailable`}</p></div>{action && <span className="journal-action-number">Action {action.ordinal + 1}</span>}</li>; })}</ol> : <p className="execution-empty">{search ? 'No recorded transitions match this search.' : 'No transitions are recorded in this journal.'}</p>}
        <details className="journal-action-register"><summary>Resource receipts ({journal.actions.length})</summary><div className="journal-table-scroll"><table><thead><tr><th>Action</th><th>Resource</th><th>Operation</th><th>Recorded state</th></tr></thead><tbody>{journal.actions.map(action => <tr key={action.ordinal}><td>{action.ordinal + 1}</td><td>{resourceName(action.resource)}</td><td>{action.operation}</td><td>{label(action.state)}</td></tr>)}</tbody></table></div></details>
      </div> : <div className="journal-captures"><p className="journal-context">Recorded failure diagnosis, captured by the deployment engine and redacted before storage. This is a snapshot, not a live log stream.</p>{journal.logs.length ? <><label>Captured container<select value={capture < journal.logs.length ? capture : 0} onChange={event => setCapture(Number(event.target.value))}>{journal.logs.map((log, index) => <option key={index} value={index}>{log.pod} / {log.container} · {log.resource.namespace || 'Cluster scope'}</option>)}</select></label>{selectedLog && <><div className="journal-console-heading"><span>{resourceName(selectedLog.resource)}</span><span>{selectedLog.lines.length} captured lines</span></div><pre className="journal-console" tabIndex={0} aria-label={`Captured output for ${selectedLog.pod} / ${selectedLog.container}`}>{selectedLog.lines.length ? selectedLog.lines.join('\n') : 'No lines were captured for this container.'}</pre></>}</> : <p className="execution-empty">No captured log output is available. Use the application’s resources view to inspect current workload logs.</p>}</div>}
      <details className="journal-raw"><summary>Raw recorded journal</summary><pre tabIndex={0} aria-label="Raw recorded journal">{JSON.stringify(journal, null, 2)}</pre></details>
    </>}
  </section>;
}
