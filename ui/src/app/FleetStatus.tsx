import type { Application } from '../api/generated';

export function matchesFleetFilter(app: Application, filter: string, disconnected = false) {
  if (filter === 'attention') return app.health === 'degraded' || app.relation === 'drifted' || ['failed', 'interrupted'].includes(app.operation ?? '');
  if (filter === 'changes') return app.relation === 'changes' || app.relation === 'drifted';
  if (filter === 'unknown') return disconnected || !app.health || app.health === 'unknown' || app.freshness.state !== 'connected';
  return true;
}

export function FleetStatus({ applications, filter, select, disconnected = false }: { applications: Application[]; filter: string; select: (filter: string) => void; disconnected?: boolean }) {
  return <div className="fleet-status" role="group" aria-label="Filter loaded applications by state">{[
    ['', 'All applications', 'Registered in this session'],
    ['attention', 'Needs attention', 'Degraded, drifted or failed'],
    ['changes', 'Changes available', 'Desired and live differ'],
    ['unknown', 'Unknown or stale', 'Observation needs checking'],
  ].map(([value, title, description]) => <button key={value} className={`fleet-status-${value || 'all'}`} aria-pressed={filter === value} onClick={() => select(value)}><span>{title}</span><strong>{applications.filter(app => matchesFleetFilter(app, value, disconnected)).length}</strong><small>{description}</small></button>)}</div>;
}
