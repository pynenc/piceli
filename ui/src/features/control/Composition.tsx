import { useState, type ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { api, applicationPath, forwardsPath, logsPath } from '../../api/client';
import { Badge, Failure, formatTime, Loading, Notice } from '../../components/State';
import './composition-harbor.css';
import { CompositionTopology } from './CompositionTopology';
import { CompositionInspector, EnvironmentChecks, EnvironmentVerification, checksSummary } from './CompositionInspector';
import { CompositionVersions } from './CompositionVersions';
import { CompositionAttention } from './CompositionAttention';
import { CompositionEnvironmentRail } from './CompositionEnvironmentRail';
import type { CompositionSelection } from './compositionGraph';
import { SourceInventory } from './SourceInventory';
import { EnvironmentHistory } from './EnvironmentHistory';

export type Component = {
  name: string; source?: string | null; commit?: string | null; digest?: string | null;
  state: string; health: string; updated_at?: string | null; reason?: string | null;
};
export type Environment = {
  name: string; namespace?: string | null; state: string; health: string;
  revision: Record<string, string>; last_sync?: string | null; reason?: string | null;
  plan_hash?: string | null; components: Component[]; application_id?: string | null;
  last_action?: string | null; verification?: Verification | null;
  trigger?: string | null; approval?: { via: string; at?: string | null } | null;
  pending_plan?: PendingPlan | null;
  checks?: EnvironmentChecksReport | null; stop?: { by: string; via?: string | null; at?: string | null } | null;
};
/** The last checks a run of the environment executed (a rollout or a verification). */
export type EnvironmentChecksReport = {
  state: string; passed?: number | null; total?: number | null; at?: string | null; trigger?: string | null;
  run_id?: string | null; action?: string | null; results: { name?: string | null; passed: boolean; code?: string | null }[];
};
/** The plan an environment waits on: exactly the hash an approval names. */
export type PendingPlan = {
  plan_hash: string; combined_hash?: string | null; release?: string | null;
  counts: Record<string, number>; changes: { operation: string; kind: string; name: string }[]; changes_total: number;
  create_namespace: boolean; stop: string[]; images: Record<string, string>;
};
/** A checks-only run against the running release: nothing applied or rolled back. */
export type Verification = { state: string; trigger?: string | null; checks_hash?: string | null; at?: string | null; failed: { check?: string | null; code?: string | null }[] };
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

function EnvironmentInventory({ environments, canSync, sync }: { environments: Environment[]; canSync: boolean; sync: ReturnType<typeof useSync> }) {
  return <div className="composition-inventory">{environments.map(env => <section className="panel composition-inventory-row" key={env.name} aria-label={`Environment ${env.name}`}>
    <div className="composition-inventory-name"><h2><Link to={environmentPath(env.name)}>{env.name}</Link></h2><p className="small muted">{env.namespace ?? 'Namespace pending'}</p></div>
    <div className="composition-inventory-state"><span className="state-label">Health / state</span><div><Badge value={env.health} /> <Badge value={env.state} /></div>{env.reason && <p className="small muted">{env.reason}</p>}</div>
    <div className="composition-inventory-revision"><span className="state-label">Revision per source</span><Revision revision={env.revision} /></div>
    <div className="composition-inventory-components"><span className="state-label">Components</span><p className="small">{componentSummary(env.components)}</p><p className="small muted">Last sync {formatTime(env.last_sync)}</p>{env.checks && <p className="small" aria-label={`Last checks of ${env.name}`}>{checksSummary(env.checks)}</p>}</div>
    <div className="composition-inventory-actions"><Link className="button" to={environmentPath(env.name)}>Open environment</Link>{canSync && <SyncButton env={env.name} sync={sync} label={`Sync ${env.name}`} />}</div>
    {env.state === 'stopped' && env.stop && <div className="composition-inventory-notice"><Notice title="Stopped">{env.stop.by === 'declared' ? 'Declared stopped in the composition (Environment(stopped=True)); remove the declaration to start it.' : `Stopped on request; start it with piceli env start ${env.name} or Start on the environment.`}</Notice></div>}
    {env.state === 'approval-required' && <div className="composition-inventory-notice"><Notice title="Approval pending">Approve the pending plan with <code>piceli gitops approve {env.name} {env.plan_hash ?? 'HASH'}</code>.</Notice></div>}
  </section>)}</div>;
}

/** Sources, versions and attention are alternate views of the same reported snapshot. */
export function CompositionOverview({ canSync }: { canSync: boolean }) {
  const query = useQuery({ queryKey: ['composition'], queryFn: ({ signal }) => api.composition(signal).then(asOverview), refetchInterval: 10000 });
  const [params, setParams] = useSearchParams();
  const sync = useSync();
  const [expanded, setExpanded] = useState(false);
  const data = query.data;
  const environments = data?.environments ?? [];
  const requestedEnvironment = params.get('environment');
  const requestedComponent = params.get('component');
  const environment = requestedEnvironment === null ? environments[0] : environments.find(item => item.name === requestedEnvironment);
  const component = requestedComponent === null ? environment?.components[0] : environment?.components.find(item => item.name === requestedComponent);
  const missingEnvironment = requestedEnvironment !== null && !environment;
  const allEnvironments = params.get('scope') === 'all';
  const view = params.get('view') === 'versions' ? 'versions' : params.get('view') === 'attention' ? 'attention' : 'topology';
  const selection: CompositionSelection | null = params.get('node') === 'source' && params.has('source') ? { kind: 'source', name: params.get('source')! } : environment && params.get('node') === 'environment' ? { kind: 'environment', name: environment.name } : component && environment ? { kind: 'component', name: component.name, environment: environment.name } : null;
  const selectedSource = data?.sources.find(item => item.name === (selection?.kind === 'source' ? selection.name : component?.source));
  const setView = (value: string) => { const next = new URLSearchParams(params); if (value === 'topology') next.delete('view'); else next.set('view', value); setParams(next, { replace: true }); };
  const selectEnvironment = (name: string) => { const next = new URLSearchParams(params); next.set('environment', name); next.delete('component'); next.delete('node'); next.delete('source'); setParams(next); };
  const selectNode = (node: CompositionSelection) => { const next = new URLSearchParams(params); next.delete('view'); next.delete('node'); next.delete('source'); if (node.kind === 'component') { next.set('environment', node.environment); next.set('component', node.name); } else if (node.kind === 'source') { next.set('node', 'source'); next.set('source', node.name); } else { next.set('node', 'environment'); next.set('environment', node.name); next.delete('component'); } setParams(next, { replace: true }); };
  const clearSelection = (scope: 'environment' | 'component') => { const next = new URLSearchParams(params); if (scope === 'environment') next.delete('environment'); next.delete('component'); next.delete('node'); next.delete('source'); setParams(next, { replace: true }); };
  const scopeEnvironment = (name: string) => { const next = new URLSearchParams(params); next.set('environment', name); for (const key of ['scope', 'component', 'node', 'source']) next.delete(key); setParams(next); };
  const explorer = (mode: 'fit' | 'explore') => data && environment ? <div className="composition-explorer">
    <CompositionEnvironmentRail environments={environments} selected={environment.name} allEnvironments={allEnvironments} onSelect={scopeEnvironment} />
    <div className="composition-landscape">
      <section className="composition-map" aria-label="Component dependencies">
        <div className="composition-map-heading"><div><h2>System schematic</h2><p>{allEnvironments ? 'All environments' : environment.namespace ?? 'Namespace pending'}</p></div><div className="composition-scope-controls"><label className="composition-environment-picker"><span className="sr-only">Environment</span><select value={environment.name} onChange={event => selectEnvironment(event.target.value)}>{environments.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}</select></label><label className="composition-all-scope"><input type="checkbox" checked={allEnvironments} onChange={event => { const next = new URLSearchParams(params); if (event.target.checked) next.set('scope', 'all'); else next.delete('scope'); setParams(next, { replace: true }); }} /> All environments</label></div></div>
        <CompositionTopology mode={mode} sources={data.sources} environments={allEnvironments ? environments : [environment]} selected={selection} onSelect={selectNode} />
        {!environment.components.length && !allEnvironments && <div className="composition-empty-map"><h3>No components reported</h3><p>Component relationships appear after the controller publishes them.</p></div>}
      </section>
      <CompositionInspector selection={selection} environment={environment} component={component} source={selectedSource} requestedComponent={requestedComponent} onClear={() => clearSelection('component')} environments={environments} sources={data.sources} onInspect={(name, componentName) => selectNode({ kind: 'component', environment: name, name: componentName })} onInspectSource={name => selectNode({ kind: 'source', name })} />
    </div>
  </div> : null;
  return <div className="composition-overview">
    <div className="heading detail-heading composition-explorer-heading"><div><h1>Your delivery landscape</h1>{data?.configured ? <div className="composition-statusline" aria-label="Composition summary"><span><strong>{environments.length}</strong> environments</span><span><strong>{environments.reduce((count, item) => count + item.components.length, 0)}</strong> components</span><span><strong>{environments.filter(item => item.state === 'approval-required').length}</strong> awaiting approval</span><span className="composition-controller-status" title={`Last controller poll: ${formatTime(data.controller?.last_poll)}`}>Controller <Badge value={data.controller?.state ?? 'unknown'} /></span></div> : <p className="subtitle">Sources, components and environments.</p>}</div><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh overview</button></div>
    {query.isPending && <Loading text="Loading composition…" />}{query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {data && !data.configured && <NotConfigured />}
    {data?.configured && <Dialog.Root open={expanded} onOpenChange={setExpanded}>
      <div className="composition-viewbar"><div className="composition-view-tabs" role="group" aria-label="Infrastructure view">{['topology', 'versions', 'attention'].map(item => <button key={item} aria-pressed={view === item} onClick={() => setView(item)}>{item[0].toUpperCase() + item.slice(1)}</button>)}</div>{view === 'topology' && environment ? <Dialog.Trigger asChild><button className="composition-expand" aria-label="Expand topology"><svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M7 2H2v5M13 2h5v5M2 13v5h5m11-5v5h-5" /></svg>Expand topology</button></Dialog.Trigger> : <span className="small muted">Controller snapshot</span>}</div>
      {view === 'versions' ? <CompositionVersions environments={environments} sources={data.sources} onInspect={(name, componentName) => selectNode({ kind: 'component', environment: name, name: componentName })} /> : view === 'attention' ? <CompositionAttention environments={environments} sources={data.sources} /> : <>
        {environment ? !expanded && explorer('fit') : missingEnvironment ? <Notice title="Selected environment unavailable"><p><code>{requestedEnvironment}</code> is not in the reported snapshot. Its selection is preserved in the address.</p><button onClick={() => clearSelection('environment')}>Reset environment selection</button></Notice> : <section className="panel empty"><h2>No environments yet</h2><p>The controller has not resolved an environment. Check its sources and refresh.</p></section>}
        <details className="composition-inventory-disclosure"><summary><span>Environment inventory</span><span>{environments.length} environments</span></summary><div className="composition-section-heading"><p className="small muted">Reported revisions, health and controller actions.</p><Link to="/composition">Open environments →</Link></div><SyncError sync={sync} /><EnvironmentInventory environments={environments} canSync={canSync} sync={sync} /></details>
      </>}
      <Dialog.Portal><Dialog.Overlay className="composition-expanded-overlay" /><Dialog.Content className="composition-expanded"><header className="composition-expanded-heading"><div><Dialog.Title>Infrastructure explorer</Dialog.Title><Dialog.Description>Select an environment or follow the connections. Escape returns to overview.</Dialog.Description></div><Dialog.Close className="composition-close">Close expanded topology <span aria-hidden="true">×</span></Dialog.Close></header>{environment ? explorer('explore') : <Notice title="Selected environment unavailable"><p>The selected environment is no longer in the controller snapshot.</p><button onClick={() => clearSelection('environment')}>Reset environment selection</button></Notice>}</Dialog.Content></Dialog.Portal>
    </Dialog.Root>}
  </div>;
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
    <EnvironmentInventory environments={query.data?.environments ?? []} canSync={canSync} sync={sync} />
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
  return <div className="composition-detail">
    <Link className="back-link" to="/composition">← Environments</Link>
    {query.isPending && <Loading text="Loading environment…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {item && <>
      <div className="heading detail-heading"><div><p className="eyebrow">Environment</p><h1>{item.name}</h1><p className="subtitle">{item.namespace ?? 'Namespace pending'}</p></div><div className="run-actions"><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh</button>{item.application_id && <><Link className="button" to={logsPath({ scope: item.application_id })}>Logs</Link><Link className="button" to={forwardsPath({ application: item.application_id })}>Forwards</Link></>}{actions?.(item)}{canSync && <SyncButton env={item.name} sync={sync} label={`Sync ${item.name}`} />}</div></div>
      <div className="statusbar" aria-label="Environment state"><div className="statusitem"><span className="statuslabel">Health</span><Badge value={item.health} /></div><div className="statusitem"><span className="statuslabel">State</span><Badge value={item.state} />{item.reason && <p>{item.reason}</p>}</div><div className="statusitem"><span className="statuslabel">Last sync</span><p>{formatTime(item.last_sync)}</p></div><div className="statusitem"><span className="statuslabel">Revision</span><Revision revision={item.revision} /></div></div><EnvironmentVerification verification={item.verification} /><EnvironmentChecks checks={item.checks} />
      <SyncError sync={sync} />
      <section className="panel control-card" aria-label="Components"><div className="panelhead"><h2>Components <span className="count">{item.components.length}</span></h2></div>
        {item.components.length === 0 ? <p className="panelbody muted">No components reported for this environment yet.</p> : <ul className="component-list">{item.components.map(component => <li key={component.name}>
          <div className="component-name"><strong>{component.name}</strong><span className="small muted">{component.source ?? 'image'} @ <code title={component.commit ?? undefined}>{shortSha(component.commit)}</code></span><code className="small muted" title={component.digest ?? undefined}>{shortDigest(component.digest)}</code></div>
          <div className="component-states"><span><span className="state-label">State</span><Badge value={component.state} /></span><span><span className="state-label">Health</span><Badge value={component.health} /></span><span><span className="state-label">Updated</span><span className="small">{formatTime(component.updated_at)}</span></span></div>
          {component.reason && <p className="small muted">{component.reason}</p>}
          {canSync && <SyncButton env={item.name} component={component.name} sync={sync} label={`Sync ${component.name}`} primary={false} />}
        </li>)}</ul>}
      </section>
      <EnvironmentHistory env={item.name} />
      <section className="panel control-card"><div className="panelhead"><h2>Workloads and logs</h2></div><div className="panelbody">{item.application_id ? <div className="run-actions"><Link className="button" to={`${applicationPath(item.application_id)}/resources`}>Open workloads, pods and logs</Link><Link className="button" to={logsPath({ scope: item.application_id })}>All logs of {item.name}</Link></div> : <p className="small muted">The namespace is not created yet; workloads appear after the first sync.</p>}</div></section>
    </>}
  </div>;
}

export function CompositionSources() {
  const query = useQuery({ queryKey: ['composition'], queryFn: ({ signal }) => api.composition(signal).then(asOverview), refetchInterval: 10000 });
  return <div className="composition-sources">
    <div className="heading"><div><h1>Sources</h1><p className="subtitle">Repositories, tracked refs and their destinations.</p></div><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh sources</button></div>
    {query.isPending && <Loading text="Loading sources…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {query.data && !query.data.configured && <NotConfigured />}
    {query.data?.configured && <ControllerLine controller={query.data.controller} />}
    {query.data?.configured && <SourceInventory sources={query.data.sources} environments={query.data.environments} />}
  </div>;
}
