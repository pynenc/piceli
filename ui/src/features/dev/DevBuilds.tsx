import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { Badge, Failure, formatTime, Loading, Notice } from '../../components/State';
import '../control/operational-harbor.css';

type DevRun = {
  run: string; requester?: string | null; priority?: string | null; profile?: string | null;
  created_at?: string | null; started_at?: string | null; finished_at?: string | null; waited_seconds?: number | null;
  position?: number | null; state?: string | null; exit_code?: number | null; reason?: string | null;
  durations?: Record<string, number> | null; tests?: { passed?: number; failed?: number; ignored?: number } | null;
  cache?: { lineage?: string | null; warm?: boolean | null; crates_compiled?: number | null; crates_locked?: number | null; hit_ratio?: number | null } | null;
};

export type DevBuildsStatus = {
  schema: string; state: 'running' | 'stale' | 'not-installed'; node?: string | null; slots?: number | null; scheduler_at?: string | null;
  running?: DevRun[]; queued?: DevRun[]; recent?: DevRun[];
  cache?: { used_bytes?: number | null; max_bytes?: number | null; at?: string | null } | null;
};

const gib = (bytes?: number | null) => bytes == null ? 'Not reported' : `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
const seconds = (value?: number | null) => value == null ? '—' : value >= 90 ? `${Math.round(value / 60)} min` : `${Math.round(value)} s`;

function phases(run: DevRun): string {
  const d = run.durations ?? {};
  const parts = ['build', 'test'].filter(name => d[name] != null).map(name => `${name} ${seconds(d[name])}`);
  return parts.length ? parts.join(' · ') : d.command != null ? `command ${seconds(d.command)}` : '—';
}

function cacheUse(run: DevRun): string {
  const cache = run.cache;
  if (!cache?.lineage) return '—';
  const hit = cache.hit_ratio == null ? '' : ` · ${Math.round(cache.hit_ratio * 100)} % fresh`;
  return `${cache.warm ? 'warm' : 'cold'}${cache.crates_compiled != null ? ` · ${cache.crates_compiled} compiled` : ''}${hit}`;
}

export function DevBuilds() {
  const query = useQuery({ queryKey: ['dev-builds'], queryFn: ({ signal }) => api.devStatus(signal).then(value => value as DevBuildsStatus), refetchInterval: 5000 });
  const status = query.data;
  const running = status?.running ?? [];
  const queued = status?.queued ?? [];
  const recent = status?.recent ?? [];
  const cache = status?.cache;
  const share = cache?.used_bytes != null && cache?.max_bytes ? Math.min(100, Math.round((cache.used_bytes / cache.max_bytes) * 100)) : null;
  return <div className="cluster-overview">
    <div className="heading detail-heading"><div><p className="eyebrow">Operations</p><h1>Development builds</h1><p className="subtitle">Build and test runs on the builder, their queue and the shared cache.</p></div><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh</button></div>
    {query.isPending && <Loading text="Loading development builds…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {status?.state === 'not-installed' && <Notice title="Development builds are not installed">Declare <code>Cluster(dev=DevBuilds(...))</code> and run <code>piceli cluster init</code>.</Notice>}
    {status && status.state !== 'not-installed' && <>
      <div className="statusbar" aria-label="Queue state">
        <div className="statusitem"><span className="statuslabel">Queue</span><Badge value={status.state === 'running' ? 'running' : 'stale'} /></div>
        <div className="statusitem"><span className="statuslabel">Builder</span><strong>{status.node ?? 'Unknown'}</strong></div>
        <div className="statusitem"><span className="statuslabel">Slots busy</span><strong>{running.length} / {status.slots ?? '?'}</strong></div>
        <div className="statusitem"><span className="statuslabel">Queued</span><strong>{queued.length}</strong></div>
        <div className="statusitem"><span className="statuslabel">Cache</span><strong>{gib(cache?.used_bytes)}{share != null ? ` (${share} %)` : ''}</strong></div>
      </div>
      {status.state === 'stale' && <Notice title="The queue is not publishing">Last update {formatTime(status.scheduler_at)}. Runs start at once until the GitOps controller (or <code>piceli dev schedule</code>) runs it again.</Notice>}
      <section className="panel control-card" aria-label="Running runs"><div className="panelhead"><h2>Running</h2></div><div className="panelbody">
        {running.length ? <ul className="control-workloads">{running.map(run => <li key={run.run}><strong>{run.run}</strong><span>{run.requester} · {run.priority} · {run.profile} · started {formatTime(run.started_at)}{run.waited_seconds != null ? ` after ${seconds(run.waited_seconds)} queued` : ''}</span></li>)}</ul> : <p className="small muted">No run is building.</p>}
      </div></section>
      <section className="panel control-card" aria-label="Queued runs"><div className="panelhead"><h2>Queue</h2></div><div className="panelbody">
        {queued.length ? <ol className="control-workloads">{queued.map(run => <li key={run.run}><strong>#{run.position} {run.run}</strong><span>{run.requester} · {run.priority} · {run.profile} · waiting {seconds(run.waited_seconds)}</span></li>)}</ol> : <p className="small muted">Nothing is waiting.</p>}
      </div></section>
      <section className="panel control-card" aria-label="Recent runs"><div className="panelhead"><h2>Recent runs</h2></div><div className="panelbody">
        {recent.length ? <ul className="control-workloads">{recent.map(run => <li key={run.run}>
          <strong>{run.run} <Badge value={run.state ?? 'unknown'} /></strong>
          <span>{run.requester} · {run.priority}{run.reason && run.state !== 'passed' ? ` · ${run.reason}` : ''} · {phases(run)}{run.tests ? ` · ${run.tests.passed ?? 0} passed, ${run.tests.failed ?? 0} failed` : ''}{run.cache?.lineage ? ` · ${cacheUse(run)}` : ''} · {formatTime(run.finished_at)}</span>
        </li>)}</ul> : <p className="small muted">No run has finished yet.</p>}
      </div></section>
    </>}
  </div>;
}
