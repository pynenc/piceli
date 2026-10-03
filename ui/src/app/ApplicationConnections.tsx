import { Link } from 'react-router-dom';
import type { Application } from '../api/generated';
import { applicationPath } from '../api/client';
import { Icon } from '../components/Icon';

export function ApplicationConnections({ application: app }: { application: Application }) {
  const path = applicationPath(app.id);
  return <section className="application-connections" aria-label="Explore application">
    <article><span className="connection-icon"><Icon name="sources" /></span><div><p className="eyebrow">01 / Definition</p><h2>{app.source ? 'Registered definition' : 'Observed scope'}</h2><p>{app.source ? `${app.source.kind} · ${app.source.entrypoint}` : 'Live inventory from this explicit Kubernetes scope.'}</p>{app.source && <code title={app.source.revision}>{app.source.revision}</code>}</div></article>
    <article><span className="connection-icon"><Icon name="cluster" /></span><div><p className="eyebrow">02 / Infrastructure</p><h2>Explore what runs</h2><p>Workloads, ownership, configuration and container images.</p><Link to={`${path}/resources?view=relationships`}>Explore resource graph <span aria-hidden="true">↗</span></Link></div></article>
    <article><span className="connection-icon"><Icon name="changes" /></span><div><p className="eyebrow">03 / Delivery</p><h2>Understand the change</h2><p>{app.capabilities?.evaluate?.allowed ? 'Review the frozen source and its exact deployment plan.' : app.capabilities?.activity?.allowed ? 'Browse saved plans, compare revisions and inspect recorded deployment logs.' : app.capabilities?.plan?.reason ?? 'Deployment review is unavailable for this scope.'}</p><div className="connection-links">{app.capabilities?.evaluate?.allowed && <Link to={`${path}/changes`}>Review changes <span aria-hidden="true">↗</span></Link>}{app.capabilities?.activity?.allowed && <><Link to={`${path}/plans`}>Previous plans <span aria-hidden="true">↗</span></Link><Link to={`${path}/activity`}>Runs &amp; logs <span aria-hidden="true">↗</span></Link><Link to={`${path}/activity?history=revisions`}>Compare revisions <span aria-hidden="true">↗</span></Link></>}</div></div></article>
  </section>;
}
