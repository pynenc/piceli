import { Link, useSearchParams } from 'react-router-dom';
import type { Resource } from '../api/generated';
import { Badge } from '../components/State';
import { ResourceKindIcon } from './ResourceTopology';
import { resourceCategory } from './ownerGraphLayout';

function object(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

/** The public owner UID projection does not imply a controller relationship. */
function controllerUids(resource: Resource) {
  if (!resource.capabilities?.manifest?.allowed) return new Set<string>();
  const references = object(resource.manifest?.metadata)?.ownerReferences;
  if (!Array.isArray(references)) return new Set<string>();
  return new Set(references.flatMap(value => {
    const reference = object(value);
    return reference?.controller === true && typeof reference.uid === 'string' ? [reference.uid] : [];
  }));
}

export function ResourceRelationships({ resource, inventory }: { resource: Resource; inventory: Resource[] }) {
  const [params] = useSearchParams();
  const scope = inventory.filter(item => item.identity.target_id === resource.identity.target_id);
  const byUid = new Map(scope.filter(item => item.identity.uid).map(item => [item.identity.uid!, item]));
  const owners = [...new Set(resource.owner_uids ?? [])];
  const children = [...new Map(scope.filter(item => resource.identity.uid && item.owner_uids?.includes(resource.identity.uid)).map(item => [item.id, item])).values()];
  const controllers = controllerUids(resource);
  const link = (item: Resource, relationship: string) => {
    const next = new URLSearchParams(params);
    next.set('resource', item.id);
    next.set('panel', 'summary');
    return <Link to={`?${next}`} className="resource-related-link" aria-label={`Inspect related ${item.identity.kind} ${item.identity.name} in ${item.identity.namespace || 'cluster scope'}`}><ResourceKindIcon kind={item.identity.kind} /><span><small>{relationship} · {item.identity.kind}</small><strong>{item.identity.name}</strong><span>{item.identity.namespace || 'Cluster scope'}</span></span><Badge value={item.health ?? 'unknown'} /><span aria-hidden="true">›</span></Link>;
  };
  return <section className="resource-evidence resource-relationships" aria-label="Related resources"><header><h4>Related resources</h4><span>UID references</span></header>
    <h5>Owners <span>{owners.length}</span></h5>{owners.length ? <ul>{owners.map(uid => <li key={uid}>{byUid.has(uid) ? link(byUid.get(uid)!, controllers.has(uid) ? 'Controller' : 'Owner') : <div className="resource-related-missing"><span>Owner outside loaded inventory</span><code>{uid}</code></div>}</li>)}</ul> : <p className="small muted">No ownership references reported.</p>}
    <h5>Direct children <span>{children.length}</span></h5>{children.length ? <ul>{children.map(item => <li key={item.id}>{link(item, 'Child')}</li>)}</ul> : <p className="small muted">{resource.identity.uid ? 'No direct children in loaded inventory.' : 'Child relationships need an observed UID.'}</p>}
    <p className="resource-evidence-note">Additional pages or unavailable scopes may contain more resources. Links follow owner UIDs, including resources outside the current filter.</p>
  </section>;
}

/** Display the service's safe condition projection, with no health inference. */
export function ResourceConditions({ resource }: { resource: Resource }) {
  if (!resource.capabilities?.conditions?.allowed) return null;
  const conditions = resource.conditions ?? [];
  return <section className="resource-evidence resource-conditions" aria-label="Condition evidence"><header><h4>Reported conditions</h4><span>{conditions.length} observed</span></header>
    {conditions.length ? <div className="resource-condition-scroll"><table aria-label="Reported conditions"><thead><tr><th>Condition</th><th>Status</th><th>Last transition</th></tr></thead><tbody>{conditions.map((condition, index) => <tr key={index}><td>{typeof condition.type === 'string' ? condition.type : 'Not reported'}</td><td><span className="resource-condition-state">{typeof condition.status === 'string' ? condition.status : 'Not reported'}</span></td><td>{typeof condition.lastTransitionTime === 'string' ? <time dateTime={condition.lastTransitionTime}>{condition.lastTransitionTime}</time> : <span className="muted">Not reported</span>}</td></tr>)}</tbody></table></div> : <p className="small muted">No conditions reported.</p>}
  </section>;
}

export function ResourceConfiguration({ resource }: { resource: Resource }) {
  const category = resourceCategory(resource.identity.kind);
  const workload = category === 'Workloads';
  const imageNames = [...new Set(resource.images ?? [])];
  const containers = [...new Set(resource.containers ?? [])];
  const ports = [...new Set(resource.ports ?? [])];
  const hasConfiguration = workload || imageNames.length > 0 || containers.length > 0 || ports.length > 0 || category === 'Networking';
  return <section className="resource-evidence resource-configuration" aria-label="Reported configuration"><header><h4>Reported configuration</h4><span>{category}</span></header>
    {!hasConfiguration && <p className="small muted">{category === 'Configuration' ? 'Configuration contents are not included in this observation.' : 'No additional configuration reported.'}</p>}
    {(workload || imageNames.length > 0) && <div className="resource-config-field"><h5>Images</h5>{imageNames.length ? <ul className="image-list">{imageNames.map(image => <li key={image}><code>{image}</code></li>)}</ul> : <p className="muted small">No image information reported.</p>}</div>}
    {(workload || containers.length > 0) && <div className="resource-config-field"><h5>Containers</h5>{containers.length ? <ul aria-label="Reported containers">{containers.map(container => <li key={container}><code>{container}</code></li>)}</ul> : <p className="muted small">No container names reported.</p>}</div>}
    {(workload || category === 'Networking' || ports.length > 0) && <div className="resource-config-field"><h5>{resource.identity.kind === 'Service' ? 'Service ports' : 'Ports'}</h5>{ports.length ? <ul aria-label="Reported ports">{ports.map(port => <li key={port}><code>{port}</code></li>)}</ul> : <p className="muted small">No ports reported.</p>}</div>}
  </section>;
}
