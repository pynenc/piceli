import type { Component, Source } from './Composition';

export type Comparison = 'same' | 'different' | 'unknown';
export const fullCommit = (value?: string | null): value is string => Boolean(value && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i.test(value));
const compare = (before?: string | null, after?: string | null): Comparison => before && after ? before === after ? 'same' : 'different' : 'unknown';

export function matchingRefs(source: Source | undefined, commit?: string | null) {
  return fullCommit(commit) ? Object.entries(source?.refs ?? {}).filter(([, value]) => fullCommit(value) && value.toLowerCase() === commit.toLowerCase()).map(([ref]) => ref) : [];
}

export function compareComponentVersions(before?: Component, after?: Component) {
  const source = compare(before?.source, after?.source);
  const commit = source === 'same' && fullCommit(before?.commit) && fullCommit(after?.commit) ? compare(before.commit.toLowerCase(), after.commit.toLowerCase()) : 'unknown';
  const digest = compare(before?.digest, after?.digest);
  const state: Comparison = !before || !after ? 'unknown' : source === 'different' || commit === 'different' || digest === 'different' ? 'different' : source === 'same' && commit === 'same' && digest === 'same' ? 'same' : 'unknown';
  return { source, commit, digest, state };
}

export function comparisonPath(baseline: string, target: string, component?: string) {
  return `/composition/overview?${new URLSearchParams({ view: 'versions', baseline, compare: target, ...(component ? { focus: component } : {}) })}`;
}

export function componentPath(environment: string, component: string) {
  return `/composition/overview?${new URLSearchParams({ environment, component })}`;
}

export function sourcePath(environment: string, source: string) {
  return `/composition/overview?${new URLSearchParams({ environment, node: 'source', source })}`;
}
