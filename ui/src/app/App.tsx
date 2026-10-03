import { lazy, Suspense, useEffect, useRef } from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { Link, NavLink, Navigate, Route, Routes, useLocation, useParams, useSearchParams } from 'react-router-dom';
import { applicationPath, api } from '../api/client';
import type { Capabilities } from '../api/generated';
import { Badge, Failure, Loading, Notice } from '../components/State';
import { compositionHome, compositionRoutes } from '../features/control/compositionRoutes';
import { Navigation } from './Navigation';
import { useUrlText } from './useUrlText';
import { Icon } from '../components/Icon';
import { Appearance } from '../components/Appearance';
import { Breadcrumb } from './Breadcrumb';
import { CommandPalette } from './CommandPalette';
import piceliLogo from '../assets/piceli-mark.svg';
import { FleetStatus, matchesFleetFilter } from './FleetStatus';
import { ApplicationConnections } from './ApplicationConnections';
import { ApplicationHeader } from './ApplicationHeader';
import { ApplicationEnvironmentContext } from './ApplicationEnvironmentContext';

const Resources = lazy(async () => ({ default: (await import('./Resources')).Resources }));
const Delivery = lazy(async () => ({ default: (await import('../features/delivery/Delivery')).Delivery }));
const PlanArchive = lazy(async () => ({ default: (await import('../features/delivery/PlanArchive')).PlanArchive }));
const DeploymentHistory = lazy(async () => ({ default: (await import('../features/delivery/DeploymentHistory')).DeploymentHistory }));
const Activity = lazy(async () => ({ default: (await import('../features/delivery/Delivery')).Activity }));
const Run = lazy(async () => ({ default: (await import('../features/delivery/Delivery')).Run }));
const Environments = lazy(async () => ({ default: (await import('../features/control/Control')).Environments }));
const GitOps = lazy(async () => ({ default: (await import('../features/control/Control')).GitOps }));
const Pipeline = lazy(async () => ({ default: (await import('../features/control/Pipeline')).Pipeline }));
const ClusterBuild = lazy(async () => ({ default: (await import('../features/control/ClusterBuild')).ClusterBuild }));
const Cluster = lazy(async () => ({ default: (await import('../features/cluster/Cluster')).Cluster }));
const LogWorkspace = lazy(async () => ({ default: (await import('../features/logs/LogWorkspace')).LogWorkspace }));
const ForwardsWorkspace = lazy(async () => ({ default: (await import('../features/access/ForwardsWorkspace')).ForwardsWorkspace }));
const ProfilePicker = lazy(async () => ({ default: (await import('../features/cluster/ProfilePicker')).ProfilePicker }));

export function App() {
  const capabilities = useQuery({ queryKey: ['capabilities'], queryFn: ({ signal }) => api.capabilities(signal) });
  const location = useLocation();
  const main = useRef<HTMLElement>(null);
  const previous = useRef(location.pathname);
  useEffect(() => {
    if (previous.current !== location.pathname) main.current?.focus();
    previous.current = location.pathname;
  }, [location.pathname]);
  return <div className="shell">
    <a className="skip" href="#main">Skip to content</a>
    <aside className="sidebar">
      <Link to={compositionHome(capabilities.data) ?? '/applications'} className="brand"><img src={piceliLogo} width="36" height="36" alt="" /><span>piceli<small>Infrastructure, connected</small></span></Link>
      <CommandPalette capabilities={capabilities.data} />
      <Navigation capabilities={capabilities.data} />
      <div className="session"><span className="avatar" aria-hidden="true">{capabilities.data?.principal.name.slice(0, 2).toUpperCase() ?? '…'}</span><div>{capabilities.data?.principal.name ?? 'Connecting'}<small>{capabilities.data?.mode === 'cluster' ? 'Scoped session' : 'Local session'}</small></div></div>
    </aside>
    <div className="workspace"><header className="topbar"><Breadcrumb /><div className="workspace-tools">{capabilities.data?.mode === 'cluster' ? <span className="hosting">Authenticated UI</span> : <Suspense fallback={<span className="hosting">Local UI</span>}><ProfilePicker /></Suspense>}<Appearance /></div></header>
      <main id="main" ref={main} tabIndex={-1}>
        {capabilities.isError && <Failure error={capabilities.error} retry={() => void capabilities.refetch()} />}
        {capabilities.data?.api_version && capabilities.data.api_version !== 'piceli.ui.v1' ? <Notice title="Service version mismatch" danger>Update the UI and service together before continuing.</Notice> : <Suspense fallback={<Loading text="Loading view…" />}><Routes>
          <Route path="/" element={capabilities.isPending ? <Loading text="Connecting…" /> : <Navigate to={compositionHome(capabilities.data) ?? '/applications'} replace />} />
          <Route path="/applications" element={<Applications capabilities={capabilities.data} />} />
          {compositionRoutes(capabilities.data)}
          <Route path="/delivery" element={capabilities.isPending ? <Loading text="Loading history capabilities…" /> : capabilities.data?.actions.composition_history?.allowed ? <DeploymentHistory environments /> : capabilities.data?.actions.activity?.allowed ? <DeploymentHistory /> : <Notice title="Deployment history unavailable">This session cannot read deployment history.</Notice>} />
          <Route path="/cluster" element={capabilities.data?.actions.cluster_status?.allowed ? <Cluster /> : <Notice title="Cluster unavailable">This session cannot read cluster status.</Notice>} />
          <Route path="/pipeline" element={capabilities.data?.actions.pipeline?.allowed ? <Pipeline /> : <Notice title="Pipeline unavailable">Start the local UI with a trusted Pipeline definition.</Notice>} />
          <Route path="/cluster-build" element={capabilities.data?.actions.cluster_build?.allowed ? <ClusterBuild /> : <Notice title="Cluster build unavailable">This installed UI has no configured cluster build for this session.</Notice>} />
          <Route path="/environments" element={capabilities.data?.actions.environments?.allowed ? <Environments canChange={capabilities.data.actions.environment_change?.allowed === true} /> : <Notice title="Environments unavailable">This service has no configured environment pipeline for this session.</Notice>} />
          <Route path="/gitops" element={capabilities.data?.actions.gitops?.allowed ? <GitOps canChange={capabilities.data.actions.gitops_change?.allowed === true} canReadEnvironments={capabilities.data.actions.environments?.allowed === true} /> : <Notice title="GitOps unavailable">This service has no controller access for this session.</Notice>} />
          <Route path="/logs" element={capabilities.isPending ? <Loading text="Loading log capabilities…" /> : capabilities.data?.actions.logs?.allowed ? <LogWorkspace capabilities={capabilities.data} /> : <Notice title="Logs unavailable">This session cannot read container logs.</Notice>} />
          <Route path="/forwards" element={capabilities.isPending ? <Loading text="Loading forward capabilities…" /> : <ForwardsWorkspace capabilities={capabilities.data} />} />
          <Route path="/applications/:applicationId" element={<Navigate to="overview" replace />} />
          <Route path="/runs/:operationId" element={<Run key={location.pathname} />} />
          <Route path="/applications/:applicationId/:tab" element={<ApplicationDetail canReadComposition={capabilities.data?.actions.composition?.allowed === true} />} />
          <Route path="*" element={<section className="empty"><h1>Page not found</h1><Link to="/applications">Open applications</Link></section>} />
        </Routes></Suspense>}
      </main>
    </div>
  </div>;
}
function Applications({ capabilities }: { capabilities?: Capabilities }) {
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useUrlText('q');
  const target = params.get('target') ?? '';
  const layout = params.get('layout') === 'table' ? 'table' : 'cards';
  const state = ['attention', 'changes', 'unknown'].includes(params.get('state') ?? '') ? params.get('state')! : '';
  const query = useInfiniteQuery({ queryKey: ['applications'], initialPageParam: null as string | null, queryFn: ({ signal, pageParam }) => api.applications(pageParam, signal), getNextPageParam: page => page.next_page ?? undefined });
  const applications = query.data?.pages.flatMap(page => page.items) ?? [];
  const filtered = applications.filter(app => matchesFleetFilter(app, state, query.isError) && (!target || app.target.id === target) && `${app.name} ${app.target.name} ${app.target.namespace} ${app.source?.revision ?? ''}`.toLowerCase().includes(search.toLowerCase()));
  const update = (key: string, value: string) => { const next = new URLSearchParams(params); if (value) next.set(key, value); else next.delete(key); setParams(next, { replace: true }); };
  return <><div className="heading detail-heading"><div><p className="eyebrow">Workspace</p><h1>Applications</h1><p className="subtitle">Your definitions, explicit targets and observed state.</p></div><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh</button></div>
    {query.data && <FleetStatus applications={applications} filter={state} disconnected={query.isError} select={value => update('state', value)} />}
    {capabilities && <RuntimeContext capabilities={capabilities} />}
    <div className="filters"><label>Search applications<input value={search} onChange={e => setSearch(e.target.value)} placeholder="Name, namespace or revision…" type="search" /></label><label>Target<select value={target} onChange={e => update('target', e.target.value)}><option value="">All targets</option>{capabilities?.targets.map(t => <option key={t.id} value={t.id}>{t.name} / {t.namespace}</option>)}</select></label><div className="segmented" role="group" aria-label="Application layout"><button aria-pressed={layout === 'cards'} onClick={() => update('layout', '')}>Cards</button><button aria-pressed={layout === 'table'} onClick={() => update('layout', 'table')}>Table</button></div></div>
    {query.isPending && <Loading />}{query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {query.data && <><p className="inventory-caption">Showing {filtered.length} of {applications.length}{query.hasNextPage ? '+' : ''} loaded applications</p>{applications.length === 0 ? <section className="panel empty"><span className="empty-icon" aria-hidden="true">▦</span><h2>Start with what you already have</h2><p>Start Piceli UI with an existing release definition and an explicit target, or open an inventory scope. Your repository structure stays yours.</p><p className="small">Use the UI command’s definition option to register a release. An inventory scope needs no definition. CI pipelines remain available through the Piceli CLI.</p></section> : filtered.length === 0 ? <section className="panel empty"><h2>No matching applications</h2><p>Change the search or target filter to see your registered scopes.</p></section> : <section className={`app-list app-layout-${layout}`} aria-label="Applications"><div className="app-list-head" aria-hidden="true"><span>Application / target</span><span>Health</span><span>Desired / live</span><span>Source</span></div>{filtered.map(app => <article className="app-row" key={app.id}><div><Link className="application-link" to={`${applicationPath(app.id)}/overview`}>{app.name}<Icon name="arrow" /></Link><p className="small muted">{app.target.name} / {app.target.namespace}</p><span className="small muted">{app.definition_kind === 'inventory' ? 'Inventory scope' : `${app.definition_kind} definition`}</span></div><div><span className="inventory-label">Health</span><Badge value={app.health ?? 'unknown'} />{(app.freshness.state !== 'connected' || query.isError) && <p className="small stale">{query.isError ? 'Stale · connection lost' : `${app.freshness.state} observation`}</p>}</div><div><span className="inventory-label">Desired / live</span><Badge value={app.relation ?? 'unknown'} /></div><div className="source-cell"><span className="inventory-label">Source</span>{app.source ? <><code>{app.source.revision}</code><p className="small muted">{app.source.kind} · {app.source.entrypoint}</p></> : <span className="muted">No source attached</span>}</div></article>)}</section>}{query.hasNextPage && <button className="load-more" onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>Load more applications</button>}</>}
  </>;
}
function RuntimeContext({ capabilities }: { capabilities: Capabilities }) {
  const cluster = capabilities.mode === 'cluster';
  const delivery = capabilities.actions.plan?.allowed === true && (capabilities.actions.deploy?.allowed === true || capabilities.actions.rollback?.allowed === true);
  const inspect = capabilities.actions.inspect?.allowed === true;
  return <section className="panel runtime-panel" aria-label="Execution environment">
    <div className="runtime-facts">
      <div><strong>UI server</strong><span>{cluster ? 'Authenticated service' : 'Local process'}</span></div>
      <div><strong>Deployment target</strong><span>{capabilities.targets.length} Kubernetes {capabilities.targets.length === 1 ? 'scope' : 'scopes'}</span></div>
      <div><strong>Available here</strong><span>{delivery ? capabilities.actions.deploy?.allowed ? 'Review and deploy' : 'Review and roll back' : inspect ? 'Observation' : 'No accessible scopes'}</span></div>
    </div>
    <details className="runtime-details"><summary>Execution environment</summary><p>{cluster ? 'Enabled actions run on this service host.' : 'Actions and port forwards run on the host serving this UI.'} Targets use explicitly configured credentials; the cluster may be on another machine.</p><p>{delivery ? 'Open an application to review its exact plan before a change.' : inspect ? 'Deployment is unavailable in this session.' : 'No application scope is authorized in this session.'}</p>{!cluster && <p>Piceli CLI can also deploy from a CI runner. This UI does not start CI jobs.</p>}</details>
  </section>;
}
function ApplicationDetail({ canReadComposition }: { canReadComposition: boolean }) {
  const { applicationId = '', tab = 'overview' } = useParams();
  const [params] = useSearchParams();
  const query = useQuery({ queryKey: ['application', applicationId], queryFn: ({ signal }) => api.application(applicationId, signal) });
  const app = query.data;
  if (!app) return query.isError ? <Failure error={query.error} retry={() => void query.refetch()} /> : <Loading text="Loading application…" />;
  if (!['overview', 'resources', 'changes', 'plans', 'activity'].includes(tab)) return <section className="empty"><h1>View unavailable</h1><Link to={`${applicationPath(app.id)}/overview`}>Open application overview</Link></section>;
  const canChanges = app.capabilities?.evaluate?.allowed === true;
  const canActivity = app.capabilities?.activity?.allowed === true;
  const canReadPlan = canActivity && Boolean(params.get('plan'));
  return <section className="application-workspace"><ApplicationHeader application={app} refreshing={query.isFetching} disconnected={query.isError} refresh={() => void query.refetch()} />
    <ApplicationEnvironmentContext applicationId={app.id} allowed={canReadComposition} />
    <nav className="tabs" aria-label="Application views"><NavLink to={`${applicationPath(app.id)}/overview`}>Overview</NavLink><NavLink to={`${applicationPath(app.id)}/resources`}>Resources</NavLink>{(canChanges || canReadPlan) && <NavLink to={`${applicationPath(app.id)}/changes${canReadPlan ? `?plan=${encodeURIComponent(params.get('plan')!)}` : ''}`}>Changes</NavLink>}{canActivity && <><NavLink to={`${applicationPath(app.id)}/plans`}>Plans</NavLink><NavLink to={`${applicationPath(app.id)}/activity`}>Activity</NavLink></>}</nav>
    {tab === 'overview' && <ApplicationConnections application={app} />}
    {(tab === 'overview' || tab === 'resources') && <Resources application={app} defaultView={tab === 'overview' ? 'relationships' : 'table'} />}{tab === 'changes' && (canChanges || canReadPlan ? <Delivery key={app.id} application={app} /> : <Notice title="Changes unavailable">No reviewed deployment action is available in this session.</Notice>)}{tab === 'plans' && (canActivity ? <PlanArchive key={app.id} application={app} /> : <Notice title="Plan history unavailable">This session cannot read saved plans.</Notice>)}{tab === 'activity' && (canActivity ? <Activity applicationId={app.id} /> : <Notice title="Activity unavailable">This service has no accessible operation history.</Notice>)}
  </section>;
}
