import { useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api, applicationPath } from '../../api/client';
import { Badge, Failure, formatTime, Loading, Notice } from '../../components/State';

export type Component = {
  name: string; source?: string | null; commit?: string | null; digest?: string | null;
  state: string; health: string; updated_at?: string | null; reason?: string | null;
};
export type Environment = {
  name: string; namespace?: string | null; state: string; health: string;
  revision: Record<string, string>; last_sync?: string | null; reason?: string | null;
  plan_hash?: string | null; components: Component[]; application_id?: string | null;
};
export type Source = { name: string; url?: string | null; refs: Record<string, string>; last_poll?: string | null; error?: string | null };
type Controller = { state?: string | null; last_poll?: string | null; poll_seconds?: number | null } | null;
type Overview = { configured: boolean; controller: Controller; sources: Source[]; environments: Environment[] };
type Detail = { configured: boolean; controller: Controller; sources: Source[]; environment: Environment };
type SyncResult = { state: string; env: string; component?: string | null; request: string };

const asOverview = (value: unknown) => value as Overview;
const asDetail = (value: unknown) => value as Detail;
const asSync = (value: unknown) => value as SyncResult;

export const shortSha = (sha?: string | null) => (sha ? sha.slice(0, 7) : 'unknown');
export const shortDigest = (digest?: string | null) => (digest ? `${digest.slice(0, 19)}…` : 'not built');
export const environmentPath = (name: string) => `/composition/environments/${encodeURIComponent(name)}`;

function Revision({ revision }: { revision: Record<string, string> }) {
  const entries = Object.entries(revision);
  if (!entries.length) return <span className="muted">Not resolved yet</span>;
  return <ul className="revision-chips" aria-label="Revision per source">{entries.map(([source, sha]) => <li key={source}><span>{source}</span><code title={sha}>{shortSha(sha)}</code></li>)}</ul>;
}

function componentSummary(components: Component[]) {
  const counts = new Map<string, number>();
  for (const item of components) counts.set(item.state, (counts.get(item.state) ?? 0) + 1);
  return [...counts.entries()].map(([state, count]) => `${count} ${state}`).join(' · ') || 'No components reported';
}

function useSync() {
  const queryClient = useQueryClient();
  const [done, setDone] = useState<Record<string, SyncResult>>({});
  const mutation = useMutation({
    mutationFn: (target: { env: string; component?: string | null }) => api.compositionSync({ env: target.env, component: target.component ?? null }).then(asSync),
    onSuccess: result => {
      setDone(current => ({ ...current, [`${result.env}/${result.component ?? ''}`]: result }));
      void queryClient.invalidateQueries({ queryKey: ['composition'] });
    },
  });
  return { mutation, done };
}

function SyncButton({ env, component, sync, label, primary = true }: { env: string; component?: string; sync: ReturnType<typeof useSync>; label: string; primary?: boolean }) {
  const key = `${env}/${component ?? ''}`;
  const pending = sync.mutation.isPending && sync.mutation.variables?.env === env && (sync.mutation.variables?.component ?? '') === (component ?? '');
  return <span className="sync-action">
    <button className={primary ? 'primary' : undefined} aria-label={label} disabled={pending} onClick={() => sync.mutation.mutate({ env, component })}>⟳ Sync</button>
    {sync.done[key] && <span className="small muted" role="status">Sync requested; the controller picks it up on its next poll.</span>}
  </span>;
}

function SyncError({ sync }: { sync: ReturnType<typeof useSync> }) {
  return sync.mutation.isError ? <Failure error={sync.mutation.error} /> : null;
}

function ControllerLine({ controller }: { controller: Controller }) {
  return <p className="small muted controller-line">Controller <Badge value={controller?.state ?? 'unknown'} /> · last poll {formatTime(controller?.last_poll)}</p>;
}

function NotConfigured() {
  return <Notice title="No GitOps controller status yet">Enable GitOps for the composition with <code>piceli gitops enable MODULE</code>. This page fills in after the controller’s first poll.</Notice>;
}

export function CompositionEnvironments({ canSync }: { canSync: boolean }) {
  const query = useQuery({ queryKey: ['composition'], queryFn: ({ signal }) => api.composition(signal).then(asOverview), refetchInterval: 10000 });
  const sync = useSync();
  return <>
    <div className="heading"><p className="eyebrow">Composition</p><h1>Environments</h1><p className="subtitle">Each environment’s revision per source, health and state, from the GitOps controller.</p></div>
    {query.isPending && <Loading text="Loading environments…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {query.data && !query.data.configured && <NotConfigured />}
    {query.data?.configured && <ControllerLine controller={query.data.controller} />}
    <SyncError sync={sync} />
    {query.data?.configured && query.data.environments.length === 0 && <section className="panel empty"><h2>No environments yet</h2><p>The controller has not resolved an environment. Check its sources and refresh.</p></section>}
    <div className="composition-grid">{query.data?.environments.map(env => <section className="panel control-card composition-card" key={env.name} aria-label={`Environment ${env.name}`}>
      <div className="panelhead"><div><h2><Link to={environmentPath(env.name)}>{env.name}</Link></h2><p className="small muted">{env.namespace ?? 'Namespace pending'}</p></div><Badge value={env.health} /></div>
      <div className="panelbody">
        <dl className="facts"><dt>State</dt><dd><Badge value={env.state} /></dd><dt>Revision</dt><dd><Revision revision={env.revision} /></dd><dt>Last sync</dt><dd>{formatTime(env.last_sync)}</dd><dt>Components</dt><dd>{componentSummary(env.components)}</dd></dl>
        {env.state === 'approval-required' && <Notice title="Approval pending">Approve the pending plan with <code>piceli gitops approve {env.name} {env.plan_hash ?? 'HASH'}</code>.</Notice>}
        <div className="run-actions"><Link className="button" to={environmentPath(env.name)}>Open environment</Link>{canSync && <SyncButton env={env.name} sync={sync} label={`Sync ${env.name}`} />}</div>
      </div>
    </section>)}</div>
  </>;
}

/**
 * One environment. ``actions`` is the header slot for environment actions
 * owned elsewhere (promote, approve, wake); it renders before Sync.
 */
export function CompositionEnvironment({ canSync, actions }: { canSync: boolean; actions?: (environment: Environment) => ReactNode }) {
  const { env = '' } = useParams();
  const query = useQuery({ queryKey: ['composition', 'environment', env], queryFn: ({ signal }) => api.compositionEnvironment(env, signal).then(asDetail), refetchInterval: 10000 });
  const sync = useSync();
  const item = query.data?.environment;
  return <>
    <Link className="back-link" to="/composition">← Environments</Link>
    {query.isPending && <Loading text="Loading environment…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {item && <>
      <div className="heading detail-heading"><div><p className="eyebrow">Environment</p><h1>{item.name}</h1><p className="subtitle">{item.namespace ?? 'Namespace pending'}</p></div><div className="run-actions"><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh</button>{actions?.(item)}{canSync && <SyncButton env={item.name} sync={sync} label={`Sync ${item.name}`} />}</div></div>
      <div className="statusbar" aria-label="Environment state"><div className="statusitem"><span className="statuslabel">Health</span><Badge value={item.health} /></div><div className="statusitem"><span className="statuslabel">State</span><Badge value={item.state} />{item.reason && <p>{item.reason}</p>}</div><div className="statusitem"><span className="statuslabel">Last sync</span><p>{formatTime(item.last_sync)}</p></div><div className="statusitem"><span className="statuslabel">Revision</span><Revision revision={item.revision} /></div></div>
      <SyncError sync={sync} />
      <section className="panel control-card" aria-label="Components"><div className="panelhead"><h2>Components <span className="count">{item.components.length}</span></h2></div>
        {item.components.length === 0 ? <p className="panelbody muted">No components reported for this environment yet.</p> : <ul className="component-list">{item.components.map(component => <li key={component.name}>
          <div className="component-name"><strong>{component.name}</strong><span className="small muted">{component.source ?? 'image'} @ <code title={component.commit ?? undefined}>{shortSha(component.commit)}</code></span><code className="small muted" title={component.digest ?? undefined}>{shortDigest(component.digest)}</code></div>
          <div className="component-states"><span><span className="state-label">State</span><Badge value={component.state} /></span><span><span className="state-label">Health</span><Badge value={component.health} /></span><span><span className="state-label">Updated</span><span className="small">{formatTime(component.updated_at)}</span></span></div>
          {component.reason && <p className="small muted">{component.reason}</p>}
          {canSync && <SyncButton env={item.name} component={component.name} sync={sync} label={`Sync ${component.name}`} primary={false} />}
        </li>)}</ul>}
      </section>
      <section className="panel control-card"><div className="panelhead"><h2>Workloads and logs</h2></div><div className="panelbody">{item.application_id ? <Link className="button" to={`${applicationPath(item.application_id)}/resources`}>Open workloads, pods and logs</Link> : <p className="small muted">The namespace is not created yet; workloads appear after the first sync.</p>}</div></section>
    </>}
  </>;
}

export function CompositionSources() {
  const query = useQuery({ queryKey: ['composition'], queryFn: ({ signal }) => api.composition(signal).then(asOverview), refetchInterval: 10000 });
  return <>
    <div className="heading"><p className="eyebrow">Composition</p><h1>Sources</h1><p className="subtitle">Git repositories the controller polls, with the commit of each ref it follows.</p></div>
    {query.isPending && <Loading text="Loading sources…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {query.data && !query.data.configured && <NotConfigured />}
    {query.data?.configured && <ControllerLine controller={query.data.controller} />}
    {query.data?.sources.map(source => <section className="panel control-card" key={source.name} aria-label={`Source ${source.name}`}>
      <div className="panelhead"><div><h2>{source.name}</h2><p className="small muted"><code>{source.url ?? 'URL not reported'}</code></p></div>{source.error ? <Badge value="failed" /> : <Badge value="connected" />}</div>
      <div className="panelbody"><dl className="facts"><dt>Last poll</dt><dd>{formatTime(source.last_poll)}</dd>{source.error && <><dt>Error</dt><dd>{source.error}</dd></>}</dl>
        <h3>Refs</h3>{Object.keys(source.refs).length ? <ul className="control-workloads">{Object.entries(source.refs).map(([ref, sha]) => <li key={ref}><strong>{ref}</strong><code title={sha}>{shortSha(sha)}</code></li>)}</ul> : <p className="small muted">No ref resolved yet.</p>}
      </div>
    </section>)}
  </>;
}
