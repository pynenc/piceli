import type { Component, Environment, Source } from './Composition';

export type CompositionNode = { id: string; kind: 'source' | 'component' | 'environment'; name: string; x: number; y: number; source?: Source; component?: Component; environment?: Environment };
export type CompositionEdge = { id: string; from: string; to: string; path: string; relation: 'source' | 'membership' };
export type CompositionGroup = { id: string; name: string; x: number; y: number; width: number; height: number };
export type CompositionSelection = { kind: 'source'; name: string } | { kind: 'environment'; name: string } | { kind: 'component'; environment: string; name: string };
export const graphCard = { width: 192, height: 64 };
// Compact columns: sources, components, environments; one row per component.
const COLUMN = [12, 252, 492];
const ROW = 78;
const TOP = 40;
export const compositionNodeId = (kind: string, ...names: string[]) => JSON.stringify([kind, ...names]);

/** No inferred runtime dependencies: only published source identity and membership. */
export function compositionGraph(sources: Source[], environments: Environment[]) {
  const nodes: CompositionNode[] = [];
  const references: Omit<CompositionEdge, 'path'>[] = [];
  const groups: CompositionGroup[] = [];
  const sourceNames = new Set<string>();
  let row = 0;
  for (const environment of environments) {
    const start = row;
    const environmentId = compositionNodeId('environment', environment.name);
    for (const component of environment.components) {
      const id = compositionNodeId('component', environment.name, component.name);
      nodes.push({ id, kind: 'component', name: component.name, component, environment, x: COLUMN[1], y: TOP + row++ * ROW });
      references.push({ id: JSON.stringify([id, environmentId]), from: id, to: environmentId, relation: 'membership' });
      if (component.source) {
        sourceNames.add(component.source);
        const sourceId = compositionNodeId('source', component.source);
        references.push({ id: JSON.stringify([sourceId, id]), from: sourceId, to: id, relation: 'source' });
      }
    }
    if (row === start) row++;
    nodes.push({ id: environmentId, kind: 'environment', name: environment.name, environment, x: COLUMN[2], y: TOP + (start + row - 1) / 2 * ROW });
    groups.push({ id: environmentId, name: environment.name, x: COLUMN[1] - 9, y: TOP - 7 + start * ROW, width: COLUMN[2] + graphCard.width - COLUMN[1] + 18, height: (row - start) * ROW - 2 });
  }
  // Published but currently unused sources remain visible as disconnected nodes.
  for (const source of sources) sourceNames.add(source.name);
  let previousSourceY = -100;
  const orderedSources = [...sourceNames].map(name => {
    const related = nodes.filter(node => node.component?.source === name);
    return { name, preferredY: related.length ? related.reduce((sum, node) => sum + node.y, 0) / related.length : Number.POSITIVE_INFINITY };
  }).sort((left, right) => left.preferredY - right.preferredY || left.name.localeCompare(right.name));
  for (const { name, preferredY: position } of orderedSources) {
    const preferredY = Number.isFinite(position) ? position : previousSourceY + ROW;
    const y = Math.max(TOP, preferredY, previousSourceY + ROW);
    nodes.push({ id: compositionNodeId('source', name), kind: 'source', name, source: sources.find(source => source.name === name), x: COLUMN[0], y });
    previousSourceY = y;
  }
  const byId = new Map(nodes.map(node => [node.id, node]));
  const edges: CompositionEdge[] = references.map(reference => {
    const from = byId.get(reference.from)!; const to = byId.get(reference.to)!;
    const x1 = from.x + graphCard.width; const y1 = from.y + graphCard.height / 2;
    const x2 = to.x; const y2 = to.y + graphCard.height / 2;
    const bend = (x1 + x2) / 2;
    return { ...reference, path: `M${x1},${y1} C${bend},${y1} ${bend},${y2} ${x2},${y2}` };
  });
  return { nodes, edges, groups, width: COLUMN[2] + graphCard.width + 12, height: Math.max(TOP + ROW, ...nodes.map(node => node.y + graphCard.height + 14)) };
}

export function selectionId(selection?: CompositionSelection | null) {
  return selection ? selection.kind === 'component' ? compositionNodeId('component', selection.environment, selection.name) : compositionNodeId(selection.kind, selection.name) : null;
}
