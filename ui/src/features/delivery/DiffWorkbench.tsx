import { useDeferredValue, useMemo, useState } from 'react';
import type { FieldChange, ResourceDiff } from '../../api/generated';
import { Notice } from '../../components/State';

type Category = 'all' | 'added' | 'changed' | 'removed' | 'other';
const categories: { key: Category; label: string; symbol: string }[] = [
  { key: 'all', label: 'All resources', symbol: '∑' },
  { key: 'added', label: 'Added', symbol: '+' },
  { key: 'changed', label: 'Changed', symbol: '~' },
  { key: 'removed', label: 'Removed', symbol: '−' },
  { key: 'other', label: 'Other', symbol: '·' },
];

function category(operation: string): Category {
  if (['add', 'create'].includes(operation)) return 'added';
  if (['change', 'update', 'patch', 'replace'].includes(operation)) return 'changed';
  if (['remove', 'delete'].includes(operation)) return 'removed';
  return 'other';
}

function valueText(value: unknown): string {
  return value === undefined ? 'Not recorded' : JSON.stringify(value, null, 2);
}

function fieldText(change: FieldChange) {
  return `${change.path} ${change.op} ${valueText(change.before)} ${valueText(change.after)}`.toLowerCase();
}

function FieldComparison({ change }: { change: FieldChange }) {
  return <div className="workbench-field" data-operation={change.op}>
    <dt><code>{change.path}</code><span className="diff-operation" data-category={change.op === 'add' ? 'added' : change.op === 'remove' ? 'removed' : 'changed'}>{change.op === 'add' ? '+ Added' : change.op === 'remove' ? '− Removed' : '~ Changed'}</span></dt>
    <dd>
      <div className="workbench-before"><span>Before</span>{change.op !== 'add' ? <pre className="removed" aria-label={`Before ${change.path}`}>{valueText(change.before)}</pre> : <p className="diff-absent">Field not present</p>}</div>
      <div className="workbench-after"><span>After</span>{change.op !== 'remove' ? <pre className="added" aria-label={`After ${change.path}`}>{valueText(change.after)}</pre> : <p className="diff-absent">Field removed</p>}</div>
    </dd>
  </div>;
}

/** Search narrows inspection only. Approval always covers the entire exact plan. */
export function DiffWorkbench({ diffs, comparison = false }: { diffs: ResourceDiff[]; comparison?: boolean }) {
  const [query, setQuery] = useState('');
  const search = useDeferredValue(query.trim().toLowerCase());
  const [filter, setFilter] = useState<Category>('all');
  const [selection, setSelection] = useState<string | null>(null);
  const indexed = useMemo(() => diffs.map((diff, index) => ({
    diff,
    key: `${diff.resource.api_version}/${diff.resource.namespace}/${diff.resource.kind}/${diff.resource.name}/${index}`,
    category: category(diff.operation),
    identityText: `${diff.resource.kind} ${diff.resource.name} ${diff.resource.namespace} ${diff.resource.api_version} ${diff.operation} ${diff.not_compared?.join(' ') ?? ''}`.toLowerCase(),
    fields: diff.changes.map(change => ({ change, text: fieldText(change) })),
  })), [diffs]);
  const counts = useMemo(() => {
    const result: Record<Category, number> = { all: diffs.length, added: 0, changed: 0, removed: 0, other: 0 };
    for (const item of indexed) result[item.category] += 1;
    return result;
  }, [diffs.length, indexed]);
  const matching = indexed.filter(item => (filter === 'all' || item.category === filter) && (!search || item.identityText.includes(search) || item.fields.some(field => field.text.includes(search))));
  const selected = matching.find(item => item.key === selection) ?? matching[0];
  const changes = selected?.fields.filter(field => !search || selected.identityText.includes(search) || field.text.includes(search)) ?? [];
  return <section className="diff-workbench" aria-label="Resource change workbench">
    <div className="diff-workbench-tools"><label><span>Search changes</span><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Resource, field or value…" /></label><p className="small muted" aria-live="polite">{matching.length} of {diffs.length} resources</p></div>
    <div className="diff-filters" role="group" aria-label="Resource operation">{categories.filter(item => item.key !== 'other' || counts.other > 0).map(item => <button key={item.key} aria-pressed={filter === item.key} data-category={item.key} onClick={() => setFilter(item.key)}><span aria-hidden="true">{item.symbol}</span>{item.label}{' '}<strong>{counts[item.key]}</strong></button>)}</div>
    {(search || filter !== 'all') && <div className="diff-filter-notice"><span>{comparison ? `Filtered view of ${diffs.length} resources across the two recorded revisions.` : `Filtered view. Approval includes all ${diffs.length} resource entries and all other plan actions.`}</span><button onClick={() => { setQuery(''); setFilter('all'); }}>Clear filters</button></div>}
    <div className="diff-workbench-grid">
      <nav className="diff-navigator" aria-label="Changed resources"><p className="eyebrow">{comparison ? 'Resources in comparison' : 'Resources in this plan'}</p>{matching.map(item => <button key={item.key} aria-label={`${item.diff.resource.name} ${item.diff.resource.kind} · ${item.diff.resource.namespace || 'Cluster scoped'} · ${item.diff.changes.length} field changes`} aria-pressed={selected?.key === item.key} onClick={() => setSelection(item.key)}><span className="diff-resource-symbol" data-category={item.category} aria-hidden="true">{categories.find(entry => entry.key === item.category)?.symbol}</span><span><strong>{item.diff.resource.name}</strong><small>{item.diff.resource.kind} · {item.diff.resource.namespace || 'Cluster scoped'}</small></span><span className="diff-resource-fields" title="Recorded field changes">{item.diff.changes.length}</span></button>)}{matching.length === 0 && <p className="small muted">No matching resources.</p>}</nav>
      <div className="diff-inspector" aria-label="Selected resource changes">
        {selected ? <><header className="diff-inspector-heading"><div><p className="eyebrow">{selected.diff.resource.api_version} · {selected.diff.resource.namespace || 'Cluster scoped'}</p><h4>{selected.diff.resource.kind} / {selected.diff.resource.name}</h4><p className="small muted">{comparison ? 'Stored desired configuration' : selected.diff.basis} · {changes.length} of {selected.diff.changes.length} field changes shown</p></div><span className="diff-operation" data-category={selected.category}>{selected.diff.operation}</span></header>
          {changes.length ? <dl className="workbench-fields">{changes.map(({ change }, index) => <FieldComparison key={`${change.path}/${index}`} change={change} />)}</dl> : <p className="diff-no-fields small muted">{selected.diff.changes.length ? 'No individual fields match this search.' : 'No individual field changes reported.'}</p>}
          {selected.diff.unified && <details className="diff-unified"><summary>Unified diff</summary><pre>{selected.diff.unified}</pre></details>}
          {(selected.diff.not_compared?.length ?? 0) > 0 && <Notice title="Fields not compared">{selected.diff.not_compared?.join(', ')}</Notice>}
        </> : <div className="empty"><h4>No changes match your filters</h4><p>Clear the filters to inspect the complete resource list.</p></div>}
      </div>
    </div>
  </section>;
}
