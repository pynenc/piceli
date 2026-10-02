import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Badge } from '../../components/State';
import { Icon } from '../../components/Icon';
import type { Environment, Source } from './Composition';
import { compositionGraph, graphCard, selectionId, type CompositionNode, type CompositionSelection } from './compositionGraph';

export function CompositionTopology({ sources, environments, selected, onSelect }: { sources: Source[]; environments: Environment[]; selected?: CompositionSelection | null; onSelect: (selection: CompositionSelection) => void }) {
  const graph = useMemo(() => compositionGraph(sources, environments), [sources, environments]);
  const viewport = useRef<HTMLDivElement>(null);
  const [availableWidth, setAvailableWidth] = useState(graph.width);
  const [zoom, setZoom] = useState<number | null>(null);
  const marker = useId().replaceAll(':', '');
  const keyboardHint = useId();
  const drag = useRef<{ id: number; x: number; y: number; left: number; top: number } | null>(null);
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const measure = () => { if (element.clientWidth) setAvailableWidth(element.clientWidth); };
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure); observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const scale = zoom ?? (availableWidth < 540 ? 1 : Math.min(1, availableWidth / graph.width));
  const activeId = selectionId(selected);
  const connected = new Set([activeId]);
  // Highlight the selected source's fan-out, or one component's complete path.
  for (const edge of graph.edges) if (edge.from === activeId || edge.to === activeId) { connected.add(edge.from); connected.add(edge.to); }
  for (const edge of graph.edges) if (selected?.kind === 'source' && connected.has(edge.from)) connected.add(edge.to);
  for (const edge of graph.edges) if (selected?.kind === 'environment' && connected.has(edge.to)) connected.add(edge.from);
  const select = (node: CompositionNode) => onSelect(node.kind === 'component' ? { kind: 'component', name: node.name, environment: node.environment!.name } : { kind: node.kind, name: node.name });
  return <div className="composition-topology">
    <div className="composition-canvas-toolbar"><span><b>{graph.nodes.length}</b> nodes <span aria-hidden="true">·</span> <b>{graph.edges.length}</b> reported connections</span><div role="group" aria-label="Topology zoom"><button aria-label="Zoom out topology" disabled={scale <= .4} onClick={() => setZoom(Math.max(.4, scale - .15))}>−</button><output aria-label="Topology zoom level">{Math.round(scale * 100)}%</output><button aria-label="Zoom in topology" disabled={scale >= 1.6} onClick={() => setZoom(Math.min(1.6, scale + .15))}>+</button><button onClick={() => { setZoom(Math.max(.4, Math.min(1, availableWidth / graph.width))); viewport.current?.scrollTo?.({ left: 0, top: 0 }); }}>Fit width</button></div></div>
    <div className="composition-graph-viewport" ref={viewport} role="region" aria-label="Infrastructure topology" aria-describedby={keyboardHint} tabIndex={0} onKeyDown={event => {
      if (event.target !== event.currentTarget) return;
      if (event.key === '+' || event.key === '=') { event.preventDefault(); setZoom(Math.min(1.6, scale + .15)); }
      else if (event.key === '-') { event.preventDefault(); setZoom(Math.max(.4, scale - .15)); }
      else if (event.key === '0') { event.preventDefault(); setZoom(1); }
    }} onPointerDown={event => {
      if (event.pointerType !== 'mouse' || event.button !== 0 || (event.target as HTMLElement).closest('button')) return;
      drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, left: event.currentTarget.scrollLeft, top: event.currentTarget.scrollTop };
      event.currentTarget.setPointerCapture?.(event.pointerId); event.currentTarget.dataset.dragging = 'true'; event.currentTarget.focus(); event.preventDefault();
    }} onPointerMove={event => {
      const start = drag.current;
      if (!start || start.id !== event.pointerId) return;
      event.currentTarget.scrollLeft = start.left - (event.clientX - start.x); event.currentTarget.scrollTop = start.top - (event.clientY - start.y);
    }} onPointerUp={event => { if (!drag.current) return; drag.current = null; delete event.currentTarget.dataset.dragging; if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); }} onPointerCancel={event => { drag.current = null; delete event.currentTarget.dataset.dragging; }}>
      <div className="composition-graph-scaled" style={{ width: graph.width * scale, height: graph.height * scale }}><div className="composition-graph-canvas" style={{ width: graph.width, height: graph.height, transform: `scale(${scale})` }}>
        <div className="composition-graph-lanes" aria-hidden="true"><span>01 <b>Sources</b></span><span>02 <b>Components</b></span><span>03 <b>Environments</b></span></div>
        <svg className="composition-graph-edges" width={graph.width} height={graph.height} aria-hidden="true"><defs><marker id={marker} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0L8 4L0 8Z" /></marker></defs>{graph.groups.map(group => <rect key={group.id} className="composition-environment-boundary" x={group.x} y={group.y} width={group.width} height={group.height} rx="12" />)}{graph.edges.map(edge => <path className={connected.has(edge.from) && connected.has(edge.to) ? 'connected' : ''} key={edge.id} d={edge.path} markerEnd={`url(#${marker})`} data-from={edge.from} data-to={edge.to} />)}</svg>
        <ul className="composition-graph-nodes">{graph.nodes.map((node, index) => <li key={node.id} style={{ left: node.x, top: node.y, width: graphCard.width, height: graphCard.height }}><button className={`composition-graph-node node-${node.kind}${connected.has(node.id) ? ' connected' : ''}`} aria-pressed={node.id === activeId} aria-label={node.kind === 'component' ? `Inspect ${node.name} in ${node.environment!.name}` : node.kind === 'source' ? `Select source ${node.name}` : `Select environment ${node.name}`} ref={element => { if (element) buttons.current.set(node.id, element); else buttons.current.delete(node.id); }} onClick={() => select(node)} onKeyDown={event => {
          let next: string | undefined;
          if (event.key === 'ArrowRight') next = graph.edges.find(edge => edge.from === node.id)?.to;
          else if (event.key === 'ArrowLeft') next = graph.edges.find(edge => edge.to === node.id)?.from;
          else if (event.key === 'ArrowDown') next = graph.nodes[Math.min(index + 1, graph.nodes.length - 1)]?.id;
          else if (event.key === 'ArrowUp') next = graph.nodes[Math.max(index - 1, 0)]?.id;
          else return;
          event.preventDefault(); if (next) buttons.current.get(next)?.focus();
        }}><span className="composition-node-kind">{node.kind === 'source' ? 'Source' : node.kind === 'component' ? node.environment!.name : 'Environment'}<Icon name={node.kind === 'source' ? 'sources' : node.kind === 'component' ? 'build' : 'environments'} /></span><strong title={node.name}>{node.name}</strong><span className="composition-node-detail">{node.kind === 'source' ? node.source ? `${Object.keys(node.source.refs).length} tracked refs` : 'Source details unavailable' : node.kind === 'component' ? node.component!.commit?.slice(0, 7) ?? 'Commit unreported' : node.environment!.namespace ?? 'Namespace pending'}</span><span className="composition-node-state">{node.kind === 'source' ? <Badge value={node.source?.error ? 'failed' : node.source?.last_poll ? 'observed' : 'unknown'} /> : <Badge value={node.component?.state ?? node.environment!.state} />}{node.kind === 'component' && <span className="small muted">{node.component!.health}</span>}</span></button></li>)}</ul>
      </div></div>
    </div><div className="composition-graph-legend"><span><i /> Source reference / environment membership</span><span id={keyboardHint}>Drag canvas to pan. Arrow keys follow nodes; Enter inspects. Canvas + / − / 0 zooms.</span></div>
  </div>;
}
