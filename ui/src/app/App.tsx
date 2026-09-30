import { lazy, Suspense, useEffect, useRef } from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { Link, NavLink, Navigate, Route, Routes, useLocation, useParams, useSearchParams } from 'react-router-dom';
import { applicationPath, api } from '../api/client';
import type { Capabilities } from '../api/generated';
import { Badge, Failure, formatTime, FreshnessNotice, Loading, Notice } from '../components/State';
import { useUrlText } from './useUrlText';

const Resources = lazy(async () => ({ default: (await import('./Resources')).Resources }));
const Delivery = lazy(async () => ({ default: (await import('../features/delivery/Delivery')).Delivery }));
const Activity = lazy(async () => ({ default: (await import('../features/delivery/Delivery')).Activity }));
const Run = lazy(async () => ({ default: (await import('../features/delivery/Delivery')).Run }));

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
    <aside className="sidebar"><Link to="/applications" className="brand"><span aria-hidden="true" className="mark">◒</span> piceli</Link>
      <p className="eyebrow nav-label">Workspace</p><nav aria-label="Main navigation"><NavLink to="/applications">▦ <span>Applications</span></NavLink></nav>
      <div className="session"><span className="avatar" aria-hidden="true">{capabilities.data?.principal.name.slice(0, 2).toUpperCase() ?? '…'}</span><div>{capabilities.data?.principal.name ?? 'Connecting'}<small>{capabilities.data?.mode === 'cluster' ? 'Scoped session' : 'Local session'}</small></div></div>
    </aside>
    <div className="workspace"><header className="topbar"><div>Workspace <span aria-hidden="true">/</span> <strong>Applications</strong></div><span className="hosting">{capabilities.data?.mode === 'cluster' ? 'Authenticated UI' : 'Local UI'}</span></header>
      <main id="main" ref={main} tabIndex={-1}>
        {capabilities.isError && <Failure error={capabilities.error} retry={() => void capabilities.refetch()} />}
        {capabilities.data?.api_version && capabilities.data.api_version !== 'piceli.ui.v1' ? <Notice title="Service version mismatch" danger>Update the UI and service together before continuing.</Notice> : <Suspense fallback={<Loading text="Loading view…" />}><Routes>
          <Route path="/" element={<Navigate to="/applications" replace />} />
          <Route path="/applications" element={<Applications capabilities={capabilities.data} />} />
          <Route path="/applications/:applicationId" element={<Navigate to="overview" replace />} />
          <Route path="/runs/:operationId" element={<Run key={location.pathname} />} />
          <Route path="/applications/:applicationId/:tab" element={<ApplicationDetail />} />
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
  const query = useInfiniteQuery({ queryKey: ['applications'], initialPageParam: null as string | null, queryFn: ({ signal, pageParam }) => api.applications(pageParam, signal), getNextPageParam: page => page.next_page ?? undefined });
  const applications = query.data?.pages.flatMap(page => page.items) ?? [];
  const filtered = applications.filter(app => (!target || app.target.id === target) && `${app.name} ${app.target.name} ${app.target.namespace} ${app.source?.revision ?? ''}`.toLowerCase().includes(search.toLowerCase()));
  const update = (key: string, value: string) => { const next = new URLSearchParams(params); if (value) next.set(key, value); else next.delete(key); setParams(next, { replace: true }); };
  return <><div className="heading"><p className="eyebrow">Delivery workspace</p><h1>Applications</h1><p className="subtitle">Your definitions, explicit targets and observed state.</p></div>
    {capabilities && <RuntimeContext capabilities={capabilities} />}
    <div className="filters"><label>Search applications<input value={search} onChange={e => setSearch(e.target.value)} placeholder="Name, namespace or revision…" type="search" /></label><label>Target<select value={target} onChange={e => update('target', e.target.value)}><option value="">All targets</option>{capabilities?.targets.map(t => <option key={t.id} value={t.id}>{t.name} / {t.namespace}</option>)}</select></label><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh</button></div>
    {query.isPending && <Loading />}{query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {query.data && <>{applications.length === 0 ? <section className="panel empty"><span className="empty-icon" aria-hidden="true">▦</span><h2>Start with what you already have</h2><p>Start Piceli UI with an existing release definition and an explicit target, or open an inventory scope. Your repository structure stays yours.</p><p className="small">Use the UI command’s definition option to register a release. An inventory scope needs no definition. CI pipelines remain available through the Piceli CLI.</p></section> : filtered.length === 0 ? <section className="panel empty"><h2>No matching applications</h2><p>Change the search or target filter to see your registered scopes.</p></section> : <section className="panel app-list" aria-label="Applications"><div className="app-list-head" aria-hidden="true"><span>Application / target</span><span>Health</span><span>Desired / live</span><span>Source</span></div>{filtered.map(app => <article className="app-row" key={app.id}><div><Link className="application-link" to={`${applicationPath(app.id)}/overview`}>{app.name}<span aria-hidden="true"> →</span></Link><p className="small muted">{app.target.name} / {app.target.namespace}</p><span className="small muted">{app.definition_kind === 'inventory' ? 'Inventory scope' : `${app.definition_kind} definition`}</span></div><div><span className="mobile-label">Health</span><Badge value={app.health ?? 'unknown'} />{(app.freshness.state !== 'connected' || query.isError) && <p className="small stale">{query.isError ? 'Stale · connection lost' : `${app.freshness.state} observation`}</p>}</div><div><span className="mobile-label">Desired / live</span><Badge value={app.relation ?? 'unknown'} /></div><div className="source-cell">{app.source ? <><code>{app.source.revision}</code><p className="small muted">{app.source.kind} · {app.source.entrypoint}</p></> : <span className="muted">No source attached</span>}</div></article>)}</section>}{query.hasNextPage && <button className="load-more" onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>Load more applications</button>}</>}
  </>;
}
function RuntimeContext({ capabilities }: { capabilities: Capabilities }) {
  const cluster = capabilities.mode === 'cluster';
  const delivery = capabilities.actions.plan?.allowed === true && capabilities.actions.deploy?.allowed === true;
  const inspect = capabilities.actions.inspect?.allowed === true;
  return <section className="panel runtime-panel" aria-label="Execution environment">
    <h2>Execution environment</h2>
    <div className="runtime-facts">
      <div><strong>UI server</strong><span>{cluster ? 'Authenticated service' : 'Local process'}</span><p>{cluster ? 'Enabled actions run on this service host.' : 'Actions and port forwards run on the host serving this UI.'}</p></div>
      <div><strong>Deployment target</strong><span>{capabilities.targets.length} Kubernetes {capabilities.targets.length === 1 ? 'scope' : 'scopes'}</span><p>Targets use explicitly configured credentials; the cluster may be on another machine.</p></div>
      <div><strong>Available here</strong><span>{delivery ? 'Review and deploy' : inspect ? 'Observation' : 'No accessible scopes'}</span><p>{delivery ? 'Open an application to review its exact plan before deployment.' : inspect ? 'Deployment is unavailable in this session.' : 'No application scope is authorized in this session.'}</p></div>
    </div>
    {!cluster && <p className="small muted">Piceli CLI can also deploy from a CI runner. This UI does not start CI jobs.</p>}
  </section>;
}
function ApplicationDetail() {
  const { applicationId = '', tab = 'overview' } = useParams();
  const query = useQuery({ queryKey: ['application', applicationId], queryFn: ({ signal }) => api.application(applicationId, signal) });
  const app = query.data;
  if (!app) return query.isError ? <Failure error={query.error} retry={() => void query.refetch()} /> : <Loading text="Loading application…" />;
  if (!['overview', 'resources', 'changes', 'activity'].includes(tab)) return <section className="empty"><h1>View unavailable</h1><Link to={`${applicationPath(app.id)}/overview`}>Open application overview</Link></section>;
  const canChanges = app.capabilities?.evaluate?.allowed === true;
  const canActivity = app.capabilities?.activity?.allowed === true;
  return <><Link className="back-link" to="/applications">← Applications</Link><div className="heading detail-heading"><div><p className="eyebrow">{app.definition_kind === 'inventory' ? 'Inventory scope' : 'Application'}</p><h1>{app.name}</h1><p className="subtitle">{app.target.name} <span aria-hidden="true">/</span> {app.target.namespace}</p></div><div className="run-actions"><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh status</button>{app.capabilities?.evaluate?.allowed && <Link className="button primary" to={`${applicationPath(app.id)}/changes`}>Review deployment</Link>}</div></div>
    <div className="context"><span>Target <strong>{app.target.name}</strong></span><span>Namespace <strong>{app.target.namespace}</strong></span><span>Ownership <strong>{app.ownership}</strong></span>{app.source && <span>Revision <code>{app.source.revision}</code></span>}</div>
    <FreshnessNotice freshness={app.freshness} disconnected={query.isError} />
    <div className="statusbar" aria-label="Application state"><Status label="Workload health" value={app.health ?? 'unknown'} detail={app.health === 'unknown' || !app.health ? 'Readiness has not been established' : undefined} /><Status label="Desired / live" value={app.relation ?? 'unknown'} detail={app.relation === 'unknown' || !app.relation ? 'No comparison available' : undefined} /><Status label="Operation" value={app.operation ?? 'idle'} /><Status label="Observation" value={query.isError ? 'stale' : app.freshness.state} detail={formatTime(app.freshness.observed_at)} /></div>
    <nav className="tabs" aria-label="Application views"><NavLink to={`${applicationPath(app.id)}/overview`}>Overview</NavLink><NavLink to={`${applicationPath(app.id)}/resources`}>Resources</NavLink>{canChanges && <NavLink to={`${applicationPath(app.id)}/changes`}>Changes</NavLink>}{canActivity && <NavLink to={`${applicationPath(app.id)}/activity`}>Activity</NavLink>}</nav>
    {tab === 'overview' && <section className="panel scope-summary"><div><h2>{app.source ? 'Registered definition' : 'Observe this scope'}</h2><p className="subtitle">{app.source ? `${app.source.kind} · ${app.source.entrypoint}` : 'Inspect observed resources without a source definition.'}</p></div><p className="small muted">{app.capabilities?.evaluate?.allowed ? 'Review the frozen source and exact deployment plan in Changes.' : app.capabilities?.plan?.reason ?? app.capabilities?.deploy?.reason ?? 'Deployment review is not available in this view.'}</p></section>}
    {(tab === 'overview' || tab === 'resources') && <Resources application={app} />}{tab === 'changes' && (canChanges ? <Delivery key={app.id} application={app} /> : <Notice title="Changes unavailable">No reviewed deployment action is available in this session.</Notice>)}{tab === 'activity' && (canActivity ? <Activity applicationId={app.id} /> : <Notice title="Activity unavailable">This service has no accessible operation history.</Notice>)}
  </>;
}
function Status({ label, value, detail }: { label: string; value: string; detail?: string }) { return <div className="statusitem"><span className="statuslabel">{label}</span><Badge value={value} />{detail && <p>{detail}</p>}</div>; }
