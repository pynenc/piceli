import { useEffect, useRef, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useVirtualizer } from '@tanstack/react-virtual';
import { Link, useSearchParams } from 'react-router-dom';
import { api, applicationPath } from '../../api/client';
import type { Capabilities, WorkspaceLogLine, WorkspaceLogSource, WorkspaceLogStream, WorkspaceScope } from '../../api/generated';
import { Failure, Loading, Notice } from '../../components/State';
import { useUrlText } from '../../app/useUrlText';
import { LEVELS, MAX_STREAMS, readFilters, scopeLabel, selectStreams, SINCE, timeLabel, workloadKey } from './workspace';
import './log-workspace.css';

type Profiles = { active: string | null; profiles: { name: string; available: boolean }[]; scopes?: WorkspaceScope[] };
const levelNames: Record<string, string> = { error: 'Error', warn: 'Warning', info: 'Info', debug: 'Debug', unknown: 'No level' };

function useDebounced<T>(value: T, delay: number): T {
  const [current, setCurrent] = useState(value);
  useEffect(() => { const timer = setTimeout(() => setCurrent(value), delay); return () => clearTimeout(timer); }, [value, delay]);
  return current;
}

/** Every readable log scope in one place: filters, live tail and bounded, redacted lines. */
export function LogWorkspace({ capabilities }: { capabilities?: Capabilities }) {
  const [params, setParams] = useSearchParams();
  const filters = readFilters(params);
  const [search, setSearch] = useUrlText('q');
  const q = useDebounced(search.trim(), 350);
  const client = useQueryClient();
  const [selectedLine, setSelectedLine] = useState<number | null>(null);
  const update = (key: string, value: string | string[] | null) => {
    const next = new URLSearchParams(params);
    next.delete(key);
    for (const item of Array.isArray(value) ? value : value ? [value] : []) next.append(key, item);
    if (key === 'workload') { next.delete('pod'); next.delete('container'); }
    if (key === 'pod') next.delete('container');
    setParams(next, { replace: true });
    setSelectedLine(null);
  };
  const canAddProfiles = capabilities?.actions.profile_scopes?.allowed === true;
  const profiles = useQuery({ queryKey: ['profiles'], queryFn: ({ signal }) => api.profiles(signal).then(value => value as Profiles), enabled: canAddProfiles });
  const registered = new Map((profiles.data?.scopes ?? []).map(scope => [`${scope.profile}:${scope.namespace}`, scope.id]));
  const addProfile = useMutation({
    mutationFn: (value: string) => { const [profile, namespace] = value.split(':'); return api.addProfileScope({ profile, namespace }); },
    onSuccess: () => { void client.invalidateQueries({ queryKey: ['profiles'] }); void client.invalidateQueries({ queryKey: ['workspace-log-sources'] }); void client.invalidateQueries({ queryKey: ['applications'] }); },
  });
  const pendingProfiles = filters.profiles.filter(value => !registered.has(value));
  useEffect(() => {
    // A shared link names other profiles' scopes; adding one only reads it.
    if (canAddProfiles && profiles.data && pendingProfiles.length && !addProfile.isPending && !addProfile.isError) addProfile.mutate(pendingProfiles[0]);
  }, [canAddProfiles, profiles.data, pendingProfiles.join(','), addProfile.isPending, addProfile.isError]); // eslint-disable-line react-hooks/exhaustive-deps
  const selectedScopes = [...filters.scopes, ...filters.profiles.map(value => registered.get(value)).filter((id): id is string => Boolean(id))];
  const sources = useQuery({ queryKey: ['workspace-log-sources', selectedScopes], queryFn: ({ signal }) => api.workspaceLogSources(selectedScopes, signal), refetchInterval: 10000, placeholderData: keepPreviousData });
  const scopes = sources.data?.scopes ?? [];
  const scopeById = new Map(scopes.map(scope => [scope.id, scope]));
  const items = sources.data?.items ?? [];
  const selection = selectStreams(items, filters, null);
  const since = SINCE.find(item => item.value === filters.since)?.seconds ?? null;
  const lines = useQuery({
    queryKey: ['workspace-log-lines', selection.streams, filters.previous, filters.tail, since, q, filters.levels],
    queryFn: ({ signal }) => api.workspaceLogLines({ stream: selection.streams, previous: filters.previous, tail_lines: filters.tail, since_seconds: since, q: q || null, level: filters.levels }, signal),
    enabled: selection.streams.length > 0,
    refetchInterval: filters.follow ? 2000 : false,
    placeholderData: keepPreviousData,
    retry: false,
  });
  const workloads = [...new Set(items.map(workloadKey))].sort();
  const pods = items.filter(item => !filters.workload || selectStreams([item], { workload: filters.workload, pod: '', container: '' }, null).sources.length);
  const containers = [...new Set(selection.sources.flatMap(item => item.containers))].sort();
  const batch = lines.data;
  const counts = new Map<string, number>();
  for (const line of batch?.lines ?? []) counts.set(line.level ?? 'unknown', (counts.get(line.level ?? 'unknown') ?? 0) + 1);
  const failed = (batch?.streams ?? []).filter(stream => stream.state !== 'ok');
  const byCluster = new Map<string, WorkspaceScope[]>();
  for (const scope of scopes) byCluster.set(scope.cluster, [...(byCluster.get(scope.cluster) ?? []), scope]);
  const toggleScope = (id: string, checked: boolean) => update('scope', checked ? [...new Set([...filters.scopes, id])] : filters.scopes.filter(item => item !== id));
  return <div className="log-workspace">
    <div className="heading detail-heading workspace-heading"><div><p className="eyebrow">Operations</p><h1>Logs</h1><p className="subtitle">Container logs from every application, environment and profile scope you can read. Reads are bounded and redacted.</p></div>
      <div className="run-actions"><button aria-pressed={filters.follow} onClick={() => update('follow', filters.follow ? '0' : null)}>{filters.follow ? 'Pause live tail' : 'Resume live tail'}</button><button onClick={() => { void sources.refetch(); void lines.refetch(); }} disabled={sources.isFetching || lines.isFetching}>Refresh</button></div></div>
    <div className="log-workspace-grid">
      <aside className="panel log-filters" aria-label="Log filters">
        <section className="log-filter-section"><h2>Scopes</h2>
          {sources.isPending && <Loading text="Finding log scopes…" />}
          {[...byCluster.entries()].map(([cluster, rows]) => <fieldset key={cluster} className="log-scope-group"><legend>{cluster}</legend>{rows.map(scope => <label key={scope.id} className="log-check"><input type="checkbox" checked={selectedScopes.includes(scope.id)} onChange={event => { const linked = filters.profiles.filter(value => registered.get(value) === scope.id); if (linked.length && !event.target.checked) update('profile', filters.profiles.filter(value => !linked.includes(value))); else toggleScope(scope.id, event.target.checked); }} /><span><strong>{scope.name}</strong><small>{scope.kind === 'environment' ? 'Environment' : scope.kind === 'profile' ? `Profile ${scope.profile}` : 'Application'} · {scope.namespace}</small></span></label>)}</fieldset>)}
          {!selectedScopes.length && scopes.length > 0 && <p className="small muted">All {scopes.length} scopes are included. Select scopes to narrow.</p>}
          {canAddProfiles && <ProfileScopeForm profiles={profiles.data} pending={addProfile.isPending} error={addProfile.isError} add={value => update('profile', [...new Set([...filters.profiles, value])])} />}
        </section>
        <section className="log-filter-section"><h2>Source</h2>
          <label>Workload<select value={filters.workload} onChange={event => update('workload', event.target.value)}><option value="">All workloads</option>{filters.workload && !workloads.includes(filters.workload) && <option value={filters.workload}>{filters.workload}</option>}{workloads.map(key => <option key={key} value={key}>{key}</option>)}</select></label>
          <label>Pod<select value={filters.pod} onChange={event => update('pod', event.target.value)}><option value="">All pods</option>{filters.pod && !pods.some(item => item.pod.name === filters.pod) && <option value={filters.pod}>{filters.pod} (not observed)</option>}{pods.map(item => <option key={`${item.scope_id}/${item.pod.uid}`} value={item.pod.name}>{item.pod.name} · {item.phase ?? 'unknown'}{item.restarts ? ` · ${item.restarts} restarts` : ''}</option>)}</select></label>
          <label>Container<select value={filters.container} onChange={event => update('container', event.target.value)}><option value="">All containers</option>{containers.map(name => <option key={name} value={name}>{name}</option>)}</select></label>
          <label className="log-check"><input type="checkbox" checked={filters.previous} onChange={event => update('previous', event.target.checked ? '1' : null)} /><span>Previous container instance</span></label>
        </section>
        <section className="log-filter-section"><h2>Window</h2>
          <label>Time range<select value={filters.since} onChange={event => update('since', event.target.value)}>{SINCE.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
          <label>Lines per container<select value={filters.tail} onChange={event => update('tail', event.target.value === '500' ? null : event.target.value)}><option value={200}>200</option><option value={500}>500</option><option value={2000}>2,000</option></select></label>
        </section>
        <fieldset className="log-filter-section log-levels"><legend>Level</legend>{LEVELS.map(level => <label key={level} className="log-check"><input type="checkbox" checked={filters.levels.includes(level)} onChange={event => update('level', event.target.checked ? [...filters.levels, level] : filters.levels.filter(item => item !== level))} /><span className={`log-level-name level-${level}`}>{levelNames[level]}</span><output aria-label={`${levelNames[level]} lines shown`}>{counts.get(level) ?? 0}</output></label>)}<p className="small muted">Levels are detected from each line’s text.</p></fieldset>
      </aside>
      <section className="panel log-stream-panel" aria-label="Log lines">
        <div className="log-toolbar"><label className="log-search"><span className="sr-only">Search log lines</span><input type="search" placeholder="Search lines…" value={search} onChange={event => setSearch(event.target.value)} /></label>
          <p className="log-summary" role="status">{selection.streams.length ? <><strong>{batch?.lines.length ?? 0}</strong> lines · {selection.streams.length} {selection.streams.length === 1 ? 'container' : 'containers'} · {selection.sources.length} {selection.sources.length === 1 ? 'pod' : 'pods'}</> : 'No containers selected'}</p>
          <span className={`log-live${filters.follow ? ' on' : ''}`}>{filters.follow ? 'Live' : 'Paused'}</span></div>
        <div className="log-notices">
          {sources.isError && <Failure error={sources.error} retry={() => void sources.refetch()} />}
          {addProfile.isError && <Notice title="Profile scope unavailable" danger>The profile could not be read. Check it with <code>piceli profiles --json</code>.</Notice>}
          {(sources.data?.partial?.length ?? 0) > 0 && <Notice title="Partial observation">Some scopes or pods could not be listed; choices may be incomplete.</Notice>}
          {selection.total > MAX_STREAMS && <Notice title={`Showing ${MAX_STREAMS} of ${selection.total} containers`}>Narrow by workload, pod or container to read the others.</Notice>}
          {failed.length > 0 && <Notice title={`${failed.length} ${failed.length === 1 ? 'container' : 'containers'} not read`}><ul className="log-failed">{failed.map(stream => <li key={`${stream.scope_id}/${stream.pod_name}/${stream.container}`}>{stream.pod_name} / {stream.container}: {stream.state === 'not-found' ? 'pod replaced or not visible' : filters.previous ? 'no previous instance' : 'unreadable'}</li>)}</ul></Notice>}
          {batch?.truncated && <Notice title="Older lines omitted">Only the newest lines are shown. Narrow the filters or the time range.</Notice>}
          {batch?.streams.some(stream => stream.gap) && <Notice title="Log history has a gap">A container produced more than one bounded read; older lines were cut.</Notice>}
          {lines.isError && <Failure error={lines.error} retry={() => void lines.refetch()} />}
        </div>
        {sources.data && !items.length && <p className="log-empty">No pods observed in the selected scopes.</p>}
        {lines.isPending && selection.streams.length > 0 && <Loading text="Reading container logs…" />}
        {batch && batch.lines.length === 0 && <p className="log-empty">No lines match these filters.</p>}
        {batch && batch.lines.length > 0 && <LogLines lines={batch.lines} streams={batch.streams} scopeNames={new Set(batch.streams.map(stream => stream.scope_id)).size > 1 ? new Map(scopes.map(scope => [scope.id, scope.name])) : null} follow={filters.follow} selected={selectedLine} select={setSelectedLine} />}
        {batch && selectedLine !== null && batch.lines[selectedLine] && <LineDetail line={batch.lines[selectedLine]} stream={batch.streams[batch.lines[selectedLine].stream]} scope={scopeById.get(batch.streams[batch.lines[selectedLine].stream]?.scope_id ?? '')} source={items.find(item => item.pod.uid === batch.streams[batch.lines[selectedLine].stream]?.pod_uid)} close={() => setSelectedLine(null)} only={pod => update('pod', pod)} />}
      </section>
    </div>
  </div>;
}

function ProfileScopeForm({ profiles, pending, error, add }: { profiles?: Profiles; pending: boolean; error: boolean; add: (value: string) => void }) {
  const others = (profiles?.profiles ?? []).filter(item => item.available);
  const [profile, setProfile] = useState('');
  const [namespace, setNamespace] = useState('default');
  if (!others.length) return null;
  const valid = Boolean(profile) && /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/.test(namespace);
  return <details className="log-profile-form" open={error || undefined}><summary>Add another profile</summary>
    <p className="small muted">Reads a namespace with that profile’s own kubeconfig and context, read-only.</p>
    <label>Profile<select value={profile} onChange={event => setProfile(event.target.value)}><option value="">Choose profile</option>{others.map(item => <option key={item.name} value={item.name}>{item.name}{item.name === profiles?.active ? ' (active)' : ''}</option>)}</select></label>
    <label>Namespace<input value={namespace} onChange={event => setNamespace(event.target.value.trim())} /></label>
    <button disabled={!valid || pending} onClick={() => add(`${profile}:${namespace}`)}>{pending ? 'Adding…' : 'Add scope'}</button>
  </details>;
}

function LogLines({ lines, streams, scopeNames, follow, selected, select }: { lines: WorkspaceLogLine[]; streams: WorkspaceLogStream[]; scopeNames: Map<string, string> | null; follow: boolean; selected: number | null; select: (index: number) => void }) {
  const scroll = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const virtual = useVirtualizer({ count: lines.length, getScrollElement: () => scroll.current, estimateSize: () => 22, overscan: 20 });
  useEffect(() => {
    if (follow && atBottom.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [lines, follow]);
  // Small reads render directly; large ones are virtualized.
  const large = lines.length > 300;
  const rows = large ? virtual.getVirtualItems().map(item => ({ index: item.index, key: String(item.key), start: item.start })) : lines.map((_, index) => ({ index, key: String(index), start: 0 }));
  return <div className={`log-lines${large ? '' : ' static'}`} ref={scroll} tabIndex={0} role="region" aria-label="Container log lines" onScroll={event => { const node = event.currentTarget; atBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 40; }}>
    <div style={large ? { height: virtual.getTotalSize(), position: 'relative' } : undefined}>{rows.map(item => {
      const line = lines[item.index];
      const stream = streams[line.stream];
      return <button key={item.key} className={`log-row level-${line.level ?? 'unknown'}${selected === item.index ? ' selected' : ''}`} style={large ? { transform: `translateY(${item.start}px)` } : undefined} aria-pressed={selected === item.index} onClick={() => select(item.index)}>
        <time dateTime={line.at ?? undefined}>{timeLabel(line.at)}</time><span className="log-level">{line.level ?? ''}</span><span className={`log-source stream-${line.stream % 6}`} title={stream ? `${scopeNames?.get(stream.scope_id) ?? ''} ${stream.pod_name} / ${stream.container}`.trim() : undefined}>{stream ? `${scopeNames ? `${scopeNames.get(stream.scope_id) ?? stream.scope_id} · ` : ''}${stream.pod_name}/${stream.container}` : '—'}</span><span className="log-text">{line.text || '\u00a0'}</span>
      </button>;
    })}</div>
  </div>;
}

function LineDetail({ line, stream, scope, source, close, only }: { line: WorkspaceLogLine; stream?: WorkspaceLogStream; scope?: WorkspaceScope; source?: WorkspaceLogSource; close: () => void; only: (pod: string) => void }) {
  return <section className="log-line-detail" aria-label="Selected log line"><div className="log-line-detail-head"><h3>Line details</h3><button onClick={close} aria-label="Close line details">✕</button></div>
    <pre>{line.text}</pre>
    <dl className="facts"><dt>Time</dt><dd>{line.at ?? 'Not stamped'}</dd><dt>Level</dt><dd>{line.level ?? 'Not detected'}</dd><dt>Scope</dt><dd>{scopeLabel(scope)}</dd><dt>Cluster</dt><dd>{scope?.cluster ?? 'Unknown'}</dd><dt>Pod</dt><dd>{stream?.pod_name}</dd><dt>Container</dt><dd>{stream?.container}{stream?.previous ? ' (previous)' : ''}</dd>{source?.workload && <><dt>Workload</dt><dd>{source.workload.kind}/{source.workload.name}</dd></>}</dl>
    <div className="run-actions">{stream && <button onClick={() => only(stream.pod_name)}>Show only this pod</button>}{scope && stream && <Link className="button" to={`${applicationPath(scope.id)}/resources?filter=${encodeURIComponent(stream.pod_name)}`}>Open pod in Resources</Link>}</div>
  </section>;
}
