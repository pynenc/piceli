import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api, applicationPath } from '../../api/client';
import { Failure, Loading, Notice } from '../../components/State';
import { Activity } from './Delivery';
import { PlanArchive } from './PlanArchive';
import './deployment-history.css';

const scopeKeys = ['compareFrom', 'compareTo', 'planSearch', 'planFilter', 'planSort', 'activity', 'activitySearch', 'plan'];

/** One visible entry point; each query still belongs to an explicitly selected application. */
export function DeploymentHistory() {
  const [params, setParams] = useSearchParams();
  const client = useQueryClient();
  const applications = useInfiniteQuery({ queryKey: ['applications'], initialPageParam: null as string | null, queryFn: ({ signal, pageParam }) => api.applications(pageParam, signal), getNextPageParam: page => page.next_page ?? undefined });
  const accessible = applications.data?.pages.flatMap(page => page.items).filter(app => app.capabilities?.activity?.allowed === true) ?? [];
  const requested = params.get('application');
  const app = requested === null ? accessible[0] : accessible.find(item => item.id === requested);
  const selected = params.get('deliveryView');
  const view = selected === 'runs' || selected === 'revisions' ? selected : 'plans';
  function selectApplication(id: string) { setParams(previous => { const next = new URLSearchParams(previous); next.set('application', id); for (const key of scopeKeys) next.delete(key); return next; }); }
  function selectView(value: string) { setParams(previous => { const next = new URLSearchParams(previous); next.set('deliveryView', value); if (app) next.set('application', app.id); return next; }); }
  function refresh() { void applications.refetch(); if (app) { void client.invalidateQueries({ queryKey: ['plans', app.id] }); void client.invalidateQueries({ queryKey: ['operations', app.id] }); } }
  return <section className="deployment-history">
    <header className="history-heading"><div><h1>Deployment history</h1><p>Saved plans, revision changes and the execution record for each deployment.</p></div><button disabled={applications.isFetching} onClick={refresh}>Refresh history</button></header>
    {applications.isPending && <Loading text="Loading application histories…" />}{applications.isError && <Failure error={applications.error} retry={() => void applications.refetch()} />}
    {applications.data && <><div className="history-scope"><label><span>Application history</span><select value={app?.id ?? ''} onChange={event => selectApplication(event.target.value)}>{!app && <option value="">Select an application</option>}{accessible.map(item => <option key={item.id} value={item.id}>{item.name} · {item.target.name} / {item.target.namespace || 'Cluster scoped'}</option>)}</select></label>{app && <Link to={`${applicationPath(app.id)}/overview`}>Open application <span aria-hidden="true">↗</span></Link>}{applications.hasNextPage && <button disabled={applications.isFetchingNextPage} onClick={() => void applications.fetchNextPage()}>Load more applications</button>}</div>
      {app ? <><nav className="history-tabs" aria-label="Deployment history views">{([['plans', 'Plans', 'Ordered steps & resource changes'], ['runs', 'Runs & logs', 'Outcomes, transitions & captured output'], ['revisions', 'Compare revisions', 'Configuration between recorded versions']] as const).map(([key, title, description]) => <button key={key} aria-pressed={view === key} onClick={() => selectView(key)}><strong>{title}</strong><span>{description}</span></button>)}</nav>
        {view === 'plans' ? <PlanArchive key={app.id} application={app} /> : <Activity key={app.id} applicationId={app.id} view={view} embedded />}
      </> : requested !== null ? <Notice title="Selected application history is unavailable">The bookmarked application is not in the loaded authorized scopes. Choose an application or load additional scopes.</Notice> : <Notice title="No accessible deployment history">This session has no application with activity access in the loaded scopes.</Notice>}
    </>}
  </section>;
}
