import type { FieldChange, JsonValue, ResourceDiff, ResourceIdentity } from '../../api/generated';

export type RevisionResource = { resource: ResourceIdentity; manifest: Record<string, JsonValue>; not_compared?: string[] };
const resourceKey = (item: RevisionResource) => JSON.stringify([item.resource.api_version, item.resource.kind, item.resource.namespace, item.resource.name]);
const pointer = (key: string) => key.replaceAll('~', '~0').replaceAll('/', '~1');
const object = (value: JsonValue | undefined): value is Record<string, JsonValue> => value !== null && typeof value === 'object' && !Array.isArray(value);

/** Compare only complete public desired snapshots. A redacted path is unknown on both sides. */
export function revisionDiff(before: RevisionResource[], after: RevisionResource[]): ResourceDiff[] {
  const left = new Map(before.map(item => [resourceKey(item), item]));
  const right = new Map(after.map(item => [resourceKey(item), item]));
  return [...new Set([...left.keys(), ...right.keys()])].sort().map(key => {
    const previous = left.get(key), next = right.get(key);
    const masks = [...new Set([...(previous?.not_compared ?? []), ...(next?.not_compared ?? [])])].sort();
    const changes: FieldChange[] = [];
    function walk(a: JsonValue | undefined, b: JsonValue | undefined, path: string) {
      if (masks.some(mask => !mask || path === mask || path.startsWith(`${mask}/`))) return;
      // Descend through maps, including additions/removals, so nested private fields
      // are never accidentally included inside a changed parent object.
      if ((object(a) || a === undefined) && (object(b) || b === undefined) && (a !== undefined || b !== undefined)) {
        const fields = [...new Set([...Object.keys(a ?? {}), ...Object.keys(b ?? {})])].sort();
        for (const field of fields) walk(a?.[field], b?.[field], `${path}/${pointer(field)}`);
        if (fields.length || (a !== undefined && b !== undefined)) return;
      }
      if (Array.isArray(a) || Array.isArray(b)) {
        if ((Array.isArray(a) || a === undefined) && (Array.isArray(b) || b === undefined)) {
          const length = Math.max(a?.length ?? 0, b?.length ?? 0);
          for (let index = 0; index < length; index++) walk(a?.[index], b?.[index], `${path}/${index}`);
          if (length || (a !== undefined && b !== undefined)) return;
        }
      }
      // A type change containing a masked descendant cannot expose that subtree.
      if (masks.some(mask => mask.startsWith(`${path}/`))) return;
      if (JSON.stringify(a) === JSON.stringify(b)) return;
      changes.push({ path: path || '/', op: a === undefined ? 'add' : b === undefined ? 'remove' : 'replace', ...(a === undefined ? {} : { before: a }), ...(b === undefined ? {} : { after: b }) });
    }
    walk(previous?.manifest, next?.manifest, '');
    return { resource: (next ?? previous)!.resource, operation: !previous ? 'create' : !next ? 'delete' : changes.length ? 'update' : masks.length ? 'not-compared' : 'unchanged', basis: 'client', changes, unified: '', not_compared: masks };
  });
}
