import type { Resource } from '../api/generated';
import type { RelatedResource } from '../features/observation/topology';

export const topologyCard = { width: 236, height: 188, columnGap: 84, rowGap: 24, inset: 24 };

export type TopologyNode = RelatedResource & {
  x: number;
  y: number;
  owners: Resource[];
  outsideOwners: number;
  parentId?: string;
  rootId: string;
};
export type TopologyEdge = { ownerId: string; resourceId: string; path: string };
export type TopologyGroup = { id: string; label: string; detail: string; x: number; y: number; width: number; height: number; resourceIds: string[] };

export function resourceCategory(kind: string) {
  if (['Deployment', 'StatefulSet', 'DaemonSet', 'ReplicaSet', 'Pod', 'Job', 'CronJob'].includes(kind)) return 'Workloads';
  if (['Service', 'Ingress', 'Endpoints', 'EndpointSlice', 'NetworkPolicy'].includes(kind)) return 'Networking';
  if (['PersistentVolumeClaim', 'PersistentVolume', 'StorageClass'].includes(kind)) return 'Storage';
  if (['ConfigMap', 'Secret', 'ServiceAccount', 'Role', 'RoleBinding', 'ClusterRole', 'ClusterRoleBinding'].includes(kind)) return 'Configuration';
  return 'Other resources';
}

/** The focused neighborhood contains direct references only, in either direction. */
export function relatedResourceIds(edges: TopologyEdge[], resourceId: string) {
  const ids = new Set([resourceId]);
  for (const edge of edges) {
    if (edge.ownerId === resourceId) ids.add(edge.resourceId);
    if (edge.resourceId === resourceId) ids.add(edge.ownerId);
  }
  return ids;
}

/** Lay out the observed ownership forest; every connector is an actual UID reference.
 * Additional owners and cycles are shown as cross-links, never inferred dependencies.
 * The caller bounds the diagram to small inventories; large scopes stay virtualized.
 */
export function layoutResourceTopology(rows: RelatedResource[]) {
  const { width, height, columnGap, rowGap, inset } = topologyCard;
  const byUid = new Map(rows.filter(row => row.resource.identity.uid).map(row => [row.resource.identity.uid!, row.resource]));
  const ancestors: TopologyNode[] = [];
  const children = new Map<string, TopologyNode[]>();
  const nodes: TopologyNode[] = rows.map(row => {
    const uids = [...new Set(row.resource.owner_uids ?? [])];
    const owners = uids.flatMap(uid => byUid.has(uid) ? [byUid.get(uid)!] : []);
    const parent = ancestors[row.depth - 1];
    const parentId = parent && owners.some(owner => owner.id === parent.resource.id) ? parent.resource.id : undefined;
    const node = { ...row, x: inset + row.depth * (width + columnGap), y: inset, owners, outsideOwners: uids.length - owners.length, parentId, rootId: parentId ? parent.rootId : row.resource.id };
    if (parentId) children.set(parentId, [...(children.get(parentId) ?? []), node]);
    ancestors[row.depth] = node;
    ancestors.length = row.depth + 1;
    return node;
  });
  const trees = new Map<string, TopologyNode[]>();
  for (const node of nodes) trees.set(node.rootId, [...(trees.get(node.rootId) ?? []), node]);
  const lanes: { id: string; label: string; tree: boolean; nodes: TopologyNode[] }[] = [];
  const independent = new Map<string, TopologyNode[]>();
  for (const [id, tree] of trees) {
    if (tree.length > 1) lanes.push({ id, label: `${tree[0].resource.identity.kind} / ${tree[0].resource.identity.name}`, tree: true, nodes: tree });
    else {
      const category = resourceCategory(tree[0].resource.identity.kind);
      independent.set(category, [...(independent.get(category) ?? []), ...tree]);
    }
  }
  for (const category of ['Workloads', 'Networking', 'Storage', 'Configuration', 'Other resources']) {
    const items = independent.get(category);
    if (items?.length) lanes.push({ id: `category:${category}`, label: category, tree: false, nodes: items });
  }
  let nextY = inset;
  const groups: TopologyGroup[] = [];
  for (const lane of lanes) {
    const groupY = nextY;
    nextY += 48;
    if (lane.tree) {
      for (const node of lane.nodes) {
        if (!children.has(node.resource.id)) { node.y = nextY; nextY += height + rowGap; }
      }
      for (const node of [...lane.nodes].reverse()) {
        const descendants = children.get(node.resource.id);
        if (descendants?.length) node.y = (descendants[0].y + descendants[descendants.length - 1].y) / 2;
      }
    } else {
      lane.nodes.forEach((node, index) => { node.x = inset + (index % 3) * (width + columnGap); node.y = nextY + Math.floor(index / 3) * (height + rowGap); });
      nextY += Math.ceil(lane.nodes.length / 3) * (height + rowGap);
    }
    groups.push({ id: lane.id, label: lane.label, detail: lane.tree ? 'Observed ownership' : 'Grouped by kind', x: 10, y: groupY, width: 0, height: nextY - groupY, resourceIds: lane.nodes.map(node => node.resource.id) });
    nextY += rowGap;
  }
  const byId = new Map(nodes.map(node => [node.resource.id, node]));
  const graphWidth = Math.max(0, ...nodes.map(node => node.x + width)) + inset;
  let categoryX = 0;
  let categoryY = groups.find(group => group.id.startsWith('category:'))?.y ?? nextY;
  let categoryHeight = 0;
  for (const group of groups) {
    group.width = graphWidth - 20;
    if (!group.id.startsWith('category:')) continue;
    const members = group.resourceIds.map(id => byId.get(id)!);
    const groupWidth = Math.max(...members.map(node => node.x + width)) + inset;
    if (categoryX && categoryX + groupWidth > graphWidth) { categoryX = 0; categoryY += categoryHeight + rowGap; categoryHeight = 0; }
    for (const node of members) { node.x += categoryX; node.y += categoryY - group.y; }
    group.x = categoryX + 10;
    group.y = categoryY;
    group.width = groupWidth - 20;
    categoryX += groupWidth + rowGap;
    categoryHeight = Math.max(categoryHeight, group.height);
  }
  if (categoryHeight) nextY = categoryY + categoryHeight + rowGap;
  const edges: TopologyEdge[] = nodes.flatMap(node => node.owners.map(owner => {
    const from = byId.get(owner.id)!;
    const x1 = from.x + width;
    const y1 = from.y + height / 2;
    const x2 = node.x;
    const y2 = node.y + height / 2;
    // Backward and self references take a visible detour around their cards.
    const path = x2 > x1
      ? `M ${x1} ${y1} C ${x1 + columnGap / 2} ${y1}, ${x2 - columnGap / 2} ${y2}, ${x2} ${y2}`
      : `M ${x1} ${y1} C ${x1 + 20} ${y1}, ${x1 + 20} ${from.y - 12}, ${from.x + width / 2} ${from.y - 12} L ${node.x + width / 2} ${node.y - 12} L ${node.x + width / 2} ${node.y}`;
    return { ownerId: owner.id, resourceId: node.resource.id, path };
  }));
  return {
    nodes,
    edges,
    groups,
    width: graphWidth,
    height: nextY,
  };
}
