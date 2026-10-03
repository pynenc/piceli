import { expect, it } from 'vitest';
import type { WorkspaceLogSource } from '../../api/generated';
import { MAX_STREAMS, matchesWorkload, readFilters, selectStreams } from './workspace';

const source = (scope: string, pod: string, workload: [string, string] | null, containers = ['api']): WorkspaceLogSource => ({
  scope_id: scope, pod: { target_id: 't', api_version: 'v1', kind: 'Pod', namespace: 'shop', name: pod, uid: `uid-${pod}` },
  workload: workload ? { kind: workload[0], name: workload[1] } : null, containers,
});

it('reads every filter from the address and ignores invalid values', () => {
  const filters = readFilters(new URLSearchParams('scope=a&scope=b&profile=west:shop&profile=../x&level=error&level=loud&since=1h&tail=9&follow=0&previous=1&q=boom'));
  expect(filters.scopes).toEqual(['a', 'b']);
  expect(filters.profiles).toEqual(['west:shop']);
  expect(filters.levels).toEqual(['error']);
  expect([filters.since, filters.tail, filters.follow, filters.previous, filters.q]).toEqual(['1h', 500, false, true, 'boom']);
  expect(readFilters(new URLSearchParams('since=3d')).since).toBe('');
});

it('matches workloads by kind and name, or by name alone from a component link', () => {
  const api = source('shop', 'api-1', ['Deployment', 'api']);
  expect(matchesWorkload(api, 'Deployment/api')).toBe(true);
  expect(matchesWorkload(api, 'StatefulSet/api')).toBe(false);
  expect(matchesWorkload(api, 'api')).toBe(true);
  expect(matchesWorkload(source('shop', 'solo', null), 'Pod/solo')).toBe(true);
});

it('selects containers for the filters and bounds one read', () => {
  const items = [source('shop', 'api-1', ['Deployment', 'api'], ['api', 'proxy']), source('env', 'web-1', ['StatefulSet', 'web'])];
  expect(selectStreams(items, { workload: '', pod: '', container: '' }, null).streams).toEqual(['shop/api-1/uid-api-1/api', 'shop/api-1/uid-api-1/proxy', 'env/web-1/uid-web-1/api']);
  expect(selectStreams(items, { workload: 'Deployment/api', pod: '', container: 'proxy' }, null).streams).toEqual(['shop/api-1/uid-api-1/proxy']);
  expect(selectStreams(items, { workload: '', pod: 'web-1', container: '' }, new Set(['shop'])).streams).toEqual([]);
  const many = Array.from({ length: 20 }, (_, index) => source('shop', `p-${index}`, null));
  const bounded = selectStreams(many, { workload: '', pod: '', container: '' }, null);
  expect(bounded.streams).toHaveLength(MAX_STREAMS);
  expect(bounded.total).toBe(20);
});
