import { Link } from 'react-router-dom';
import { applicationPath } from '../api/client';
import type { Application } from '../api/generated';
import { Badge, formatTime, FreshnessNotice } from '../components/State';
import { Icon } from '../components/Icon';
import './application-workspace.css';

/** Keep identity and observation in view without pushing the workspace below them. */
export function ApplicationHeader({ application: app, refreshing, disconnected, refresh }: { application: Application; refreshing: boolean; disconnected: boolean; refresh: () => void }) {
  return <header className="application-header">
    <div className="application-titlebar">
      <div className="application-title"><Link className="application-back" to="/applications" aria-label="Back to applications">←</Link><span className="application-symbol" aria-hidden="true"><Icon name="applications" /></span><div><p className="eyebrow">{app.definition_kind === 'inventory' ? 'Inventory scope' : 'Application'}</p><h1>{app.name}</h1></div></div>
      <div className="run-actions"><button onClick={refresh} disabled={refreshing}>Refresh status</button>{app.capabilities?.evaluate?.allowed && <Link className="button primary" to={`${applicationPath(app.id)}/changes`}>Review deployment</Link>}</div>
    </div>
    <div className="application-coordinate"><span>Target <strong>{app.target.name}</strong></span><span>Namespace <strong>{app.target.namespace}</strong></span><span>Ownership <strong>{app.ownership}</strong></span>{app.source && <span>Revision <code>{app.source.revision}</code></span>}</div>
    <div className="application-status" aria-label="Application state">
      <span><span className="statuslabel">Workload health</span><Badge value={app.health ?? 'unknown'} />{(!app.health || app.health === 'unknown') && <span className="sr-only">Readiness has not been established</span>}</span>
      <span><span className="statuslabel">Desired / live</span><Badge value={app.relation ?? 'unknown'} />{(!app.relation || app.relation === 'unknown') && <span className="sr-only">No comparison available</span>}</span>
      <span><span className="statuslabel">Operation</span><Badge value={app.operation ?? 'idle'} /></span>
      <span><span className="statuslabel">Observation</span><Badge value={disconnected ? 'stale' : app.freshness.state} /><time className="application-observed" dateTime={app.freshness.observed_at ?? undefined}>{formatTime(app.freshness.observed_at)}</time></span>
    </div>
    <FreshnessNotice freshness={app.freshness} disconnected={disconnected} />
  </header>;
}
