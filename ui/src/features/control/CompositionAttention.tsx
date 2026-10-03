import { Link } from 'react-router-dom';
import type { Environment, Source } from './Composition';

type Attention = { id: string; title: string; scope: string; detail: string; path: string; category: 'investigate' | 'decision' | 'lifecycle' };
export function compositionAttention(sources: Source[], environments: Environment[]) {
  const signals: Attention[] = sources.filter(source => source.error).map(source => ({ id: `source/${source.name}`, title: 'Source read failed', scope: source.name, detail: source.error!, path: '/composition/sources', category: 'investigate' }));
  for (const environment of environments) {
    const path = `/composition/environments/${encodeURIComponent(environment.name)}`;
    if (['degraded', 'failed', 'unavailable'].includes(environment.health) || ['failed', 'error', 'interrupted'].includes(environment.state)) signals.push({ id: `health/${environment.name}`, title: 'Environment needs investigation', scope: environment.name, detail: environment.reason ?? `${environment.state} · health ${environment.health}`, path, category: 'investigate' });
    if (environment.state === 'approval-required') signals.push({ id: `approval/${environment.name}`, title: 'Plan awaiting approval', scope: environment.name, detail: environment.plan_hash ? `Exact plan ${environment.plan_hash}` : 'The controller has not published a plan hash.', path, category: 'decision' });
    if (['stopped', 'suspended', 'pending', 'building', 'rolling', 'progressing'].includes(environment.state)) signals.push({ id: `state/${environment.name}`, title: environment.state === 'stopped' ? 'Environment stopped' : environment.state === 'suspended' ? 'Environment suspended' : 'Environment progressing', scope: environment.name, detail: environment.reason ?? environment.state, path, category: 'lifecycle' });
    for (const component of environment.components) {
      const issue = ['degraded', 'failed', 'unavailable'].includes(component.health) || ['failed', 'error', 'interrupted'].includes(component.state);
      if (issue || ['pending', 'building', 'rolling', 'progressing'].includes(component.state)) signals.push({ id: `component/${environment.name}/${component.name}`, title: issue ? 'Component needs investigation' : 'Component in progress', scope: `${environment.name} / ${component.name}`, detail: component.reason ?? `${component.state} · health ${component.health}`, path: `/composition/overview?${new URLSearchParams({ environment: environment.name, component: component.name })}`, category: issue ? 'investigate' : 'lifecycle' });
    }
  }
  return signals;
}

export function CompositionAttention({ sources, environments }: { sources: Source[]; environments: Environment[] }) {
  const signals = compositionAttention(sources, environments);
  const groups = [{ id: 'investigate', title: 'Needs investigation', description: 'Reported source errors and unhealthy components.' }, { id: 'decision', title: 'Decisions waiting', description: 'Review gates that need your explicit approval.' }, { id: 'lifecycle', title: 'In progress & stopped', description: 'Reported lifecycle states, separate from failures.' }] as const;
  return <div className="composition-attention">{groups.map(group => <section className={`panel attention-${group.id}`} key={group.id} aria-label={group.title}><div className="panelhead"><div><p className="eyebrow">{group.id === 'investigate' ? 'Observe' : group.id === 'decision' ? 'Review' : 'Follow'}</p><h2>{group.title} <span className="count">{signals.filter(signal => signal.category === group.id).length}</span></h2><p className="small muted">{group.description}</p></div></div>{signals.filter(signal => signal.category === group.id).length ? <ul>{signals.filter(signal => signal.category === group.id).map(signal => <li key={signal.id}><div><span>{signal.scope}</span><h3>{signal.title}</h3><p>{signal.detail}</p></div><Link to={signal.path}>Inspect <span aria-hidden="true">→</span></Link></li>)}</ul> : <p className="panelbody muted small">No {group.id === 'investigate' ? 'issues' : group.id === 'decision' ? 'approval requests' : 'lifecycle changes'} reported in this snapshot.</p>}</section>)}</div>;
}
