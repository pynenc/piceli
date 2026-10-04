import type { WorkspaceLogSource, WorkspaceScope } from '../../api/generated';

export const LEVELS = ['error', 'warn', 'info', 'debug', 'unknown'] as const;
export const SINCE: { value: string; label: string; seconds: number | null }[] = [
  { value: '', label: 'Newest lines', seconds: null },
  { value: '5m', label: 'Last 5 minutes', seconds: 300 },
  { value: '15m', label: 'Last 15 minutes', seconds: 900 },
  { value: '1h', label: 'Last hour', seconds: 3600 },
  { value: '6h', label: 'Last 6 hours', seconds: 21600 },
  { value: '24h', label: 'Last 24 hours', seconds: 86400 },
];
export const MAX_STREAMS = 12;

export type LogFilters = {
  scopes: string[]; profiles: string[]; workload: string; pod: string; container: string;
  previous: boolean; since: string; levels: string[]; q: string; tail: number; follow: boolean;
};

/** Every filter lives in the address, so a link or reload restores the same view. */
export function readFilters(params: URLSearchParams): LogFilters {
  const tail = Number(params.get('tail'));
  return {
    scopes: params.getAll('scope'),
    profiles: params.getAll('profile').filter(value => /^[a-z0-9][a-z0-9._-]{0,62}:[a-z0-9][-a-z0-9]{0,62}$/.test(value)),
    workload: params.get('workload') ?? '',
    pod: params.get('pod') ?? '',
    container: params.get('container') ?? '',
    previous: params.get('previous') === '1',
    since: SINCE.some(item => item.value === params.get('since')) ? params.get('since')! : '',
    levels: params.getAll('level').filter(level => (LEVELS as readonly string[]).includes(level)),
    q: params.get('q') ?? '',
    tail: [200, 500, 2000].includes(tail) ? tail : 500,
    follow: params.get('follow') !== '0',
  };
}

export const workloadKey = (source: WorkspaceLogSource) => source.workload ? `${source.workload.kind}/${source.workload.name}` : `Pod/${source.pod.name}`;

/** ``Deployment/api`` matches that workload; a bare ``api`` matches any kind with that name. */
export function matchesWorkload(source: WorkspaceLogSource, workload: string) {
  if (!workload) return true;
  const key = workloadKey(source);
  return workload.includes('/') ? key === workload : key.split('/')[1] === workload || source.pod.name === workload;
}

export type Selection = { sources: WorkspaceLogSource[]; streams: string[]; total: number };

/** The containers the filters select, bounded to what one read may request. */
export function selectStreams(items: WorkspaceLogSource[], filters: Pick<LogFilters, 'workload' | 'pod' | 'container'>, scopes: Set<string> | null): Selection {
  const sources = items.filter(item => (!scopes || scopes.has(item.scope_id)) && matchesWorkload(item, filters.workload) && (!filters.pod || item.pod.name === filters.pod));
  const all = sources.flatMap(item => item.containers.filter(name => !filters.container || name === filters.container).map(container => `${item.scope_id}/${item.pod.name}/${item.pod.uid}/${container}`));
  return { sources, streams: all.slice(0, MAX_STREAMS), total: all.length };
}

export function scopeLabel(scope: WorkspaceScope | undefined) {
  if (!scope) return 'Unknown scope';
  return `${scope.name} · ${scope.namespace}`;
}

export function timeLabel(at?: string | null) {
  if (!at) return '—';
  const date = new Date(at.replace(/(\.\d{3})\d*Z$/, '$1Z'));
  if (Number.isNaN(date.valueOf())) return '—';
  return `${date.toLocaleTimeString([], { hour12: false })}.${String(date.getMilliseconds()).padStart(3, '0')}`;
}
