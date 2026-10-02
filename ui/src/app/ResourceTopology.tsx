import { useId, useMemo, useRef, useState } from 'react';
import type { Resource } from '../api/generated';
import { Badge } from '../components/State';
import type { RelatedResource } from '../features/observation/topology';
import { layoutResourceTopology, relatedResourceIds, resourceCategory, topologyCard, type TopologyNode } from './ownerGraphLayout';
import './resources.css';

function ownerDescription(node: TopologyNode) {
  const known = node.owners.map(owner => `${owner.identity.kind} ${owner.identity.name}`);
  if (node.outsideOwners) known.push(`${node.outsideOwners} ${node.outsideOwners === 1 ? 'owner' : 'owners'} outside this view`);
  return known.length ? `Owner${known.length > 1 ? 's' : ''}: ${known.join(', ')}` : 'No owner reference';
}

function ResourceNodeEvidence({ resource, id }: { resource: Resource; id: string }) {
  const images = [...new Set(resource.images ?? [])];
  const containers = new Set(resource.containers ?? []).size;
  const ports = [...new Set(resource.ports ?? [])];
  const facts = [containers ? `${containers} ${containers === 1 ? 'container' : 'containers'}` : '', ports.length ? `${ports.length === 1 ? 'Port' : 'Ports'} ${ports.join(', ')}` : ''].filter(Boolean).join(' · ');
  const fallback = resource.phase ? `Phase ${resource.phase}` : resourceCategory(resource.identity.kind) === 'Configuration' ? 'Configuration metadata' : 'No configuration reported';
  return <span id={id} className="resource-node-evidence">{images.length > 0 && <code title={images.join(', ')}>{images[0]}{images.length > 1 ? ` +${images.length - 1}` : ''}</code>}<span title={facts || fallback}>{facts || fallback}</span></span>;
}

export function ResourceKindIcon({ kind }: { kind: string }) {
  const category = resourceCategory(kind);
  return <svg className="resource-kind-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {kind === 'Pod' ? <><path d="m12 2 9 5v10l-9 5-9-5V7zM3 7l9 5 9-5M12 12v10" /><path d="m7.5 4.5 9 5" /></> : category === 'Workloads' ? <><rect x="3" y="3" width="14" height="14" rx="3" /><path d="M8 21h10a3 3 0 0 0 3-3V8M7 8h6M7 12h4" /></> : category === 'Networking' ? <><circle cx="12" cy="5" r="3" /><rect x="2" y="16" width="6" height="5" rx="1" /><rect x="16" y="16" width="6" height="5" rx="1" /><path d="M12 8v4M5 16v-4h14v4" /></> : category === 'Storage' ? <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0" /></> : kind === 'Secret' ? <><rect x="4" y="10" width="16" height="12" rx="3" /><path d="M7 10V7a5 5 0 0 1 10 0v3M12 15v3" /></> : <><path d="M14 3H5v18h14V8zM14 3v5h5M8 12h8M8 16h5" /></>}
  </svg>;
}

export function ResourceTopology({ rows, onSelect, selectedId }: { rows: RelatedResource[]; onSelect: (resource: Resource) => void; selectedId?: string | null }) {
  const graph = useMemo(() => layoutResourceTopology(rows), [rows]);
  const instructions = useId();
  const marker = useId().replaceAll(':', '');
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const viewport = useRef<HTMLDivElement>(null);
  const [focusedId, setFocusedId] = useState('');
  const [zoom, setZoom] = useState(1);
  const focused = graph.nodes.find(node => node.resource.id === (selectedId || focusedId));
  const childCount = focused ? graph.edges.filter(edge => edge.ownerId === focused.resource.id).length : 0;
  const related = focused ? relatedResourceIds(graph.edges, focused.resource.id) : null;
  const adjustZoom = (value: number) => setZoom(Math.max(.25, Math.min(1.5, Math.round(value * 100) / 100)));
  const fit = () => {
    const element = viewport.current;
    if (!element) return;
    adjustZoom(Math.min(1, (element.clientWidth - 24) / graph.width, (element.clientHeight - 24) / graph.height));
    element.scrollTo?.({ top: 0, left: 0 });
  };
  return <div className="resource-topology">
    <div className="resource-graph-header"><div className="resource-topology-caption"><div><strong>Observed ownership</strong><p id={instructions} className="sr-only">Follow owner references from workloads to their children. Select a resource to inspect it.</p></div><span>{graph.nodes.length} resources · {graph.edges.length} references</span></div>
    <div className="resource-graph-tools"><label>Focus resource<select value={focused?.resource.id ?? ''} onChange={event => { setFocusedId(event.target.value); buttons.current.get(event.target.value)?.scrollIntoView?.({ block: 'nearest', inline: 'center' }); }}><option value="">All resources</option>{graph.nodes.map(node => <option key={node.resource.id} value={node.resource.id}>{node.resource.identity.kind} / {node.resource.identity.name}</option>)}</select></label><div role="group" aria-label="Graph zoom"><button onClick={() => adjustZoom(zoom - .1)} disabled={zoom <= .25} aria-label="Zoom out">−</button><output aria-label="Graph scale">{Math.round(zoom * 100)}%</output><button onClick={() => adjustZoom(zoom + .1)} disabled={zoom >= 1.5} aria-label="Zoom in">+</button><button onClick={fit}>Fit graph</button><button onClick={() => adjustZoom(1)}>Reset zoom</button></div></div>
    </div>
    <div ref={viewport} className="resource-topology-scroll" role="region" aria-label="Resource list" aria-describedby={instructions} tabIndex={0} style={{ height: Math.min(620, Math.max(360, graph.height + 24)) }}>
      <p className="sr-only">Use Up and Down to move between resources, Left for an owner, or Right for a child. Press Enter to inspect.</p>
      <div className="resource-graph-scaler" style={{ width: graph.width * zoom, height: graph.height * zoom }}><div className="resource-topology-canvas" style={{ width: graph.width, height: graph.height, transform: `scale(${zoom})` }}>
        {graph.groups.map(group => <div key={group.id} className="resource-graph-lane" style={{ top: group.y, left: group.x, width: group.width, height: group.height }}><strong>{group.label}</strong><span title={group.detail}>{group.resourceIds.length} {group.resourceIds.length === 1 ? 'resource' : 'resources'}</span></div>)}
        <svg className="resource-topology-lines" width={graph.width} height={graph.height} aria-hidden="true" focusable="false">
          <defs><marker id={marker} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M 0 0 L 8 4 L 0 8 Z" /></marker></defs>
          {graph.edges.map(edge => <path key={`${edge.ownerId}/${edge.resourceId}`} d={edge.path} markerEnd={`url(#${marker})`} data-owner={edge.ownerId} data-resource={edge.resourceId} className={focused ? edge.ownerId === focused.resource.id || edge.resourceId === focused.resource.id ? 'is-related' : 'is-dimmed' : undefined} />)}
        </svg>
        <ul className="resource-topology-nodes">
          {graph.nodes.map((node, index) => {
            const resource = node.resource;
            const owner = ownerDescription(node);
            return <li key={resource.id} style={{ left: node.x, top: node.y, width: topologyCard.width, height: topologyCard.height }}>
              <button className={`resource-row resource-topology-node${focused?.resource.id === resource.id ? ' is-selected' : related?.has(resource.id) ? ' is-related' : focused ? ' is-dimmed' : ''}`} aria-label={`Inspect ${resource.identity.kind} ${resource.identity.name} in ${resource.identity.namespace || 'cluster scope'}`} aria-describedby={`${marker}-states-${index} ${marker}-owner-${index} ${marker}-facts-${index}`} ref={element => { if (element) buttons.current.set(resource.id, element); else buttons.current.delete(resource.id); }} onFocus={() => setFocusedId(resource.id)} onClick={() => onSelect(resource)} onKeyDown={event => {
                let next: string | undefined;
                if (event.key === 'ArrowLeft') next = node.parentId ?? node.owners[0]?.id;
                else if (event.key === 'ArrowRight') next = graph.nodes.find(child => child.parentId === resource.id)?.resource.id;
                else if (event.key === 'ArrowDown') next = graph.nodes[Math.min(index + 1, graph.nodes.length - 1)]?.resource.id;
                else if (event.key === 'ArrowUp') next = graph.nodes[Math.max(index - 1, 0)]?.resource.id;
                else if (event.key === 'Home') next = graph.nodes[0]?.resource.id;
                else if (event.key === 'End') next = graph.nodes.at(-1)?.resource.id;
                else return;
                event.preventDefault();
                if (next) buttons.current.get(next)?.focus();
              }}>
                <span className="resource-node-heading"><ResourceKindIcon kind={resource.identity.kind} /><span className="kind">{resource.identity.kind}</span><span><span className="sr-only">Health</span><Badge value={resource.health ?? 'unknown'} /></span></span>
                <span className="resource-topology-identity"><strong title={resource.identity.name}>{resource.identity.name}</strong><span className="small muted" title={`${resource.identity.namespace || 'Cluster scope'} · ${resource.ownership}`}>{resource.identity.namespace || 'Cluster scope'} · {resource.ownership}</span></span>
                <ResourceNodeEvidence resource={resource} id={`${marker}-facts-${index}`} />
                <span className="resource-topology-states" id={`${marker}-states-${index}`}><span><span className="state-label">Presence</span><Badge value={resource.presence} /></span><span><span className="state-label">Desired / live</span><Badge value={resource.relation ?? 'unknown'} /></span><span className="sr-only">Health {resource.health ?? 'unknown'}</span></span>
                <span className="sr-only" id={`${marker}-owner-${index}`}>{owner}</span>
              </button>
            </li>;
          })}
        </ul>
      </div></div>
    </div>
    <div className="resource-graph-footer" role="status" aria-label="Graph focus">{focused ? <><strong>{focused.resource.identity.kind} / {focused.resource.identity.name}</strong><span>{focused.owners.length} observed {focused.owners.length === 1 ? 'owner' : 'owners'} · {childCount} direct {childCount === 1 ? 'child' : 'children'}{focused.outsideOwners ? ` · ${focused.outsideOwners} owners outside this view` : ''}</span><div className="resource-focus-actions"><button aria-label={`Open details for ${focused.resource.identity.kind} ${focused.resource.identity.name}`} onClick={() => onSelect(focused.resource)}>Inspect resource</button><button onClick={() => setFocusedId('')}>Clear focus</button></div></> : <><span><i aria-hidden="true" /> Kubernetes owner reference</span><span>Grouped by kind; service and traffic connections are not inferred.</span></>}</div>
  </div>;
}
