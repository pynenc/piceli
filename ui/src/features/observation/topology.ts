import type { Resource } from '../../api/generated';

export type RelatedResource = { resource: Resource; depth: number };

/** Owner-ordered view of the same resource identities used by the table. */
export function orderByOwner(resources: Resource[]): RelatedResource[] {
  const byUid = new Map(resources.filter(item => item.identity.uid).map(item => [item.identity.uid!, item]));
  const children = new Map<string, Resource[]>();
  const roots: Resource[] = [];
  for (const resource of resources) {
    const owner = resource.owner_uids?.find(uid => byUid.has(uid));
    if (!owner) roots.push(resource);
    else children.set(owner, [...(children.get(owner) ?? []), resource]);
  }
  const compare = (left: Resource, right: Resource) => `${left.identity.kind}/${left.identity.name}`.localeCompare(`${right.identity.kind}/${right.identity.name}`);
  const ordered: RelatedResource[] = [];
  const seen = new Set<string>();
  const visit = (resource: Resource, depth: number) => {
    const stack: RelatedResource[] = [{ resource, depth }];
    while (stack.length) {
      const item = stack.pop()!;
      if (seen.has(item.resource.id)) continue;
      seen.add(item.resource.id);
      ordered.push(item);
      const descendants = children.get(item.resource.identity.uid ?? '') ?? [];
      for (const child of descendants.slice().sort(compare).reverse()) stack.push({ resource: child, depth: item.depth + 1 });
    }
  };
  for (const resource of roots.slice().sort(compare)) visit(resource, 0);
  for (const resource of resources.slice().sort(compare)) visit(resource, 0);
  return ordered;
}
