import { useState } from 'react';
import { formatTime, label } from '../../components/State';
import { matchedAction, resourceName, stateDescription, type ExecutionJournal, type ExecutionStep } from './executionEvidence';
import './execution-evidence.css';

export function PlanExecutionFlow({ steps = [], journal, recorded = false }: { steps?: ExecutionStep[]; journal?: ExecutionJournal | null; recorded?: boolean }) {
  const [selected, setSelected] = useState<number | null>(null);
  const ordered = [...steps].sort((a, b) => a.ordinal - b.ordinal);
  const actions = new Map(journal?.actions.map(action => [action.ordinal, action]));
  const state = (step: ExecutionStep) => recorded ? matchedAction(step, actions.get(step.ordinal))?.state ?? 'unreported' : 'planned';
  const inspected = ordered.find(step => step.ordinal === selected) ?? ordered[0];
  const inspectedAction = inspected && matchedAction(inspected, actions.get(inspected.ordinal));
  const groups: { level: number | null; steps: ExecutionStep[] }[] = [];
  for (const step of ordered) {
    const last = groups.at(-1);
    const level = step.level ?? null;
    if (last && last.level === level) last.steps.push(step);
    else groups.push({ level, steps: [step] });
  }
  const counts = new Map<string, number>();
  for (const step of ordered) counts.set(state(step), (counts.get(state(step)) ?? 0) + 1);
  return <section className="execution-flow" aria-label="Execution order">
    <header className="execution-flow-heading"><div><p className="eyebrow">{recorded ? 'Resource execution' : 'Before you deploy'}</p><h3>Execution order</h3><p>Frozen dependency phases in action order. A shared phase does not promise parallel execution.</p></div><span>{ordered.length} {ordered.length === 1 ? 'action' : 'actions'}</span></header>
    {!ordered.length ? <p className="execution-empty">No dependency order is reported for this plan. The recorded actions and checks remain available below.</p> : <>
      {recorded && <div className="execution-state-counts" aria-label="Recorded resource states">{[...counts].map(([name, count]) => <span key={name} data-state={name}>{count} {name}</span>)}</div>}
      <div className="execution-flow-workspace"><ol className="execution-phases" aria-label="Planned dependency phases">{groups.map((group, groupIndex) => <li key={`${group.level}/${groupIndex}`}><div className="execution-phase-label"><span aria-hidden="true">{group.level === null ? '—' : String(group.level + 1).padStart(2, '0')}</span><div><h4>{group.level === null ? 'Additional actions' : `Phase ${group.level + 1}`}</h4><p>{group.level === null ? 'Outside dependency levels' : `${group.steps.length} ${group.steps.length === 1 ? 'resource' : 'resources'}`}</p></div></div><div className="execution-phase-actions">{group.steps.map(step => <button key={step.ordinal} aria-label={`Inspect ${recorded ? 'recorded' : 'planned'} action ${step.ordinal + 1}: ${step.resource.kind} ${step.resource.name}`} aria-pressed={step.ordinal === inspected?.ordinal} onClick={() => setSelected(step.ordinal)} data-state={state(step)}><span className="execution-action-order">{step.ordinal + 1}</span><span className="execution-action-resource"><small>{step.resource.kind} · {step.resource.namespace || 'Cluster scope'}</small><strong>{step.resource.name}</strong><span>{step.operation}</span></span><span className="execution-action-state">{label(state(step))}</span></button>)}</div></li>)}</ol>
        {inspected && <section className="execution-action-detail" aria-label="Selected action details"><p className="eyebrow">Action {inspected.ordinal + 1}</p><h4>{inspected.resource.kind} / {inspected.resource.name}</h4><p className="execution-action-explanation">{stateDescription(state(inspected))}</p><dl><dt>Operation</dt><dd>{inspected.operation}</dd><dt>Namespace</dt><dd>{inspected.resource.namespace || 'Cluster scope'}</dd><dt>API version</dt><dd><code>{inspected.resource.api_version}</code></dd><dt>Recorded state</dt><dd>{label(state(inspected))}</dd>{inspectedAction?.written_at && <><dt>Write recorded</dt><dd>{formatTime(inspectedAction.written_at)}</dd></>}</dl><h5>Depends on</h5>{inspected.dependencies?.length ? <ul>{inspected.dependencies.map((resource, index) => <li key={index}>{resourceName(resource)}</li>)}</ul> : <p className="execution-action-explanation">No resource dependencies are recorded for this action.</p>}<details><summary>Exact resource identity</summary><pre>{JSON.stringify(inspected.resource, null, 2)}</pre></details></section>}
      </div>
    </>}
  </section>;
}
