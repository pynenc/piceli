import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api, ApiError, applicationPath, basePath } from '../../api/client';
import type { Capabilities, ForwardEntry } from '../../api/generated';
import { Badge, Failure, formatTime, Loading, Notice } from '../../components/State';
import '../logs/log-workspace.css';

const ACTIVE = ['ready', 'connecting', 'pending'];
const staleText: Record<string, string> = { 'scope-removed': 'Its scope was removed', reconnecting: 'Reconnecting after a failed probe' };

function message(error: Error) {
  return error instanceof ApiError ? error.detail.message : 'Refresh and try again.';
}

/** Every forward and connection ticket of this session, across scopes. */
export function ForwardsWorkspace({ capabilities }: { capabilities?: Capabilities }) {
  const client = useQueryClient();
  const forwards = useQuery({ queryKey: ['forwards'], queryFn: ({ signal }) => api.forwards(signal), refetchInterval: 3000 });
  const page = forwards.data;
  const refresh = () => { void client.invalidateQueries({ queryKey: ['forwards'] }); void client.invalidateQueries({ queryKey: ['navigation'] }); };
  const stop = useMutation({ mutationFn: (entry: ForwardEntry) => page?.mode === 'cluster' ? api.stopRemoteAccess(entry.session.application_id, entry.session.id) : api.stopAccess(entry.session.application_id, entry.session.id), onSuccess: refresh, retry: false });
  const stale = useMutation({ mutationFn: () => api.stopStaleForwards(), onSuccess: refresh, retry: false });
  const items = page?.items ?? [];
  const active = items.filter(item => ACTIVE.includes(item.session.state));
  const ended = items.filter(item => !ACTIVE.includes(item.session.state));
  const staleCount = items.filter(item => item.stale).length + (page?.orphans ?? 0);
  const cluster = page?.mode === 'cluster';
  return <div className="forwards-workspace">
    <div className="heading detail-heading workspace-heading"><div><p className="eyebrow">Operations</p><h1>Port forwards</h1><p className="subtitle">{cluster ? 'Connection tickets for piceli ui connect on your own machine, across every scope you may access.' : 'Supervised loopback forwards on the host serving this UI, across applications, environments and profiles.'}</p></div><button onClick={() => void forwards.refetch()} disabled={forwards.isFetching}>Refresh</button></div>
    {forwards.isPending && <Loading text="Loading forwards…" />}
    {forwards.isError && <Failure error={forwards.error} retry={() => void forwards.refetch()} />}
    {page?.mode === 'unavailable' && <Unavailable reason={page.reason} composition={capabilities?.actions.composition?.allowed === true} />}
    {page && page.mode !== 'unavailable' && <>
      {staleCount > 0 && <Notice title={`${staleCount} stale ${staleCount === 1 ? 'forward' : 'forwards'}`}><p>{page.orphans ? `${page.orphans} left running by a previous Piceli UI process. ` : ''}Stopping stale forwards ends only Piceli’s own recorded processes and forwards whose scope was removed; nothing else is signalled.</p>{!cluster && <button onClick={() => stale.mutate()} disabled={stale.isPending}>{stale.isPending ? 'Stopping…' : 'Stop stale forwards'}</button>}{stale.isSuccess && <p className="small" role="status">Stopped {stale.data.stopped}.</p>}{stale.isError && <p className="small" role="alert">{message(stale.error)}</p>}</Notice>}
      {capabilities?.actions.composition?.allowed && <p className="small muted forward-cli">Every Service port of an environment from your machine: <code>piceli access ENV --cluster infra.py:CLUSTER</code>.</p>}
      <div className="forward-grid">
        <section className="panel" aria-label="Active forwards"><div className="panelhead"><h2>Active <span className="count">{active.length}</span></h2></div>
          {stop.isError && <div className="log-notices"><Notice title="Forward did not stop" danger>{message(stop.error)}</Notice></div>}
          {active.length ? <ul className="forward-list">{active.map(entry => <ForwardRow key={entry.session.id} entry={entry} cluster={cluster} stop={() => stop.mutate(entry)} stopping={stop.isPending} />)}</ul> : <p className="forward-empty">No active forwards. Start one here or from a workload’s <strong>Forward</strong> action.</p>}
          {ended.length > 0 && <details className="forward-ended"><summary>Recently ended ({ended.length})</summary><ul className="forward-list">{ended.map(entry => <ForwardRow key={entry.session.id} entry={entry} cluster={cluster} />)}</ul></details>}
        </section>
        <StartForward cluster={cluster} done={refresh} />
      </div>
    </>}
  </div>;
}

function Unavailable({ reason, composition }: { reason?: string | null; composition: boolean }) {
  if (reason === 'kubectl-unavailable') return <Notice title="Forwards unavailable on this host">Install <code>kubectl</code> on the host serving this UI, then restart <code>piceli ui serve</code>.</Notice>;
  return <Notice title={composition ? 'Forwards start on your machine' : 'Forwards are not configured'}>
    {composition ? <><p>This UI runs inside the cluster and cannot open a port on your machine. From your machine, run <code>piceli access APP</code> for an application’s declared forwards, or <code>piceli ui serve --profile NAME</code> to start and stop forwards from this page locally.</p><p>Workloads and their logs remain available here: open <Link to="/logs">Logs</Link> or an environment’s workloads.</p></> : <p>This service has no forward or connection-ticket support for this session.</p>}
  </Notice>;
}

function ForwardRow({ entry, cluster, stop, stopping = false }: { entry: ForwardEntry; cluster: boolean; stop?: () => void; stopping?: boolean }) {
  const session = entry.session;
  const where = session.binding_location === 'server' ? 'On the UI host' : session.binding_location === 'local_client' ? 'On your machine (client-reported)' : 'Through a gateway';
  return <li data-forward-state={session.state}>
    <div><Badge value={session.state} />{entry.stale && <span className="forward-stale" title={staleText[entry.stale_reason ?? ''] ?? undefined}>Stale · {staleText[entry.stale_reason ?? ''] ?? entry.stale_reason}</span>}</div>
    <div><strong>{session.resource.kind}/{session.resource.name}</strong><small>{entry.application_name}{entry.scope ? ` · ${entry.scope.namespace} · ${entry.scope.cluster}` : ''}</small></div>
    <div><strong>{session.local_port ?? '—'} → {session.remote_port}</strong><small>{where} · expires {formatTime(session.expires_at)}</small>{entry.url ? <a href={entry.url} target="_blank" rel="noreferrer"><code>{entry.url}</code></a> : cluster && session.state === 'pending' ? <small>Waiting for piceli ui connect</small> : null}</div>
    <div className="run-actions">{entry.scope && <Link className="button" to={`${applicationPath(session.application_id)}/resources`}>Workloads</Link>}{stop && entry.session.application_id && entry.stale_reason !== 'scope-removed' && <button onClick={stop} disabled={stopping} aria-label={`Stop forward ${session.resource.name} ${session.local_port ?? session.remote_port}`}>Stop</button>}</div>
  </li>;
}

function StartForward({ cluster, done }: { cluster: boolean; done: () => void }) {
  const [params, setParams] = useSearchParams();
  const applicationId = params.get('application') ?? '';
  const resourceId = params.get('resource') ?? '';
  const applications = useQuery({ queryKey: ['applications', 'forward-targets'], queryFn: ({ signal }) => api.applications(null, signal) });
  const resources = useQuery({ queryKey: ['resources', applicationId, 'forward-targets'], queryFn: ({ signal }) => api.resources(applicationId, null, signal), enabled: Boolean(applicationId) });
  const targets = (resources.data?.items ?? []).filter(item => item.capabilities?.access?.allowed);
  const resource = targets.find(item => item.id === resourceId) ?? (resourceId ? undefined : targets[0]);
  const requestedPort = Number(params.get('port'));
  const remotePort = resource?.ports?.includes(requestedPort) ? requestedPort : resource?.ports?.[0] ?? 0;
  const [localPort, setLocalPort] = useState<number | null>(null);
  const local = localPort ?? (remotePort >= 1024 ? remotePort : 8080);
  const set = (key: string, value: string) => { const next = new URLSearchParams(params); if (value) next.set(key, value); else next.delete(key); if (key === 'application') { next.delete('resource'); next.delete('port'); } if (key === 'resource') next.delete('port'); setParams(next, { replace: true }); setLocalPort(null); };
  const start = useMutation({
    mutationFn: async () => {
      if (!resource) throw new Error('No target');
      const base = { resource_id: resource.id, resource_uid: resource.identity.uid ?? '', remote_port: remotePort };
      if (cluster) return { ticket: await api.issueRemoteAccess(applicationId, base) };
      return { session: await api.startAccess(applicationId, { ...base, local_port: local }) };
    },
    onSuccess: done,
    retry: false,
  });
  const valid = Boolean(resource?.identity.uid && remotePort && Number.isInteger(local) && local >= 1 && local <= 65535);
  const server = `${window.location.origin}${basePath()}`;
  const ticket = start.data && 'ticket' in start.data ? start.data.ticket : undefined;
  return <section className="panel forward-start" aria-label="Start a forward"><h2>{cluster ? 'Issue a connection ticket' : 'Start a forward'}</h2>
    <label>Scope<select value={applicationId} onChange={event => set('application', event.target.value)}><option value="">Choose application or environment</option>{applications.data?.items.map(app => <option key={app.id} value={app.id}>{app.name} · {app.target.namespace}</option>)}</select></label>
    {applicationId && resources.isPending && <Loading text="Finding forwardable targets…" />}
    {resources.isError && <Failure error={resources.error} retry={() => void resources.refetch()} />}
    {applicationId && resources.data && !targets.length && <p className="small muted">No Service, Deployment or Pod with a forwardable port is visible in this scope.</p>}
    {targets.length > 0 && <label>Target<select value={resource?.id ?? ''} onChange={event => set('resource', event.target.value)}>{!resource && <option value="">Target unavailable</option>}{targets.map(item => <option key={item.id} value={item.id}>{item.identity.kind}/{item.identity.name}</option>)}</select></label>}
    {resource && <label>Target port<select value={remotePort} onChange={event => set('port', event.target.value)}>{resource.ports?.map(port => <option key={port} value={port}>{port}</option>)}</select></label>}
    {resource && <label>{cluster ? 'Port on your machine' : 'Local port on the UI host'}<input type="number" min={1} max={65535} value={local} onChange={event => setLocalPort(Number(event.target.value))} /></label>}
    <button className="primary" disabled={!valid || start.isPending} onClick={() => start.mutate()}>{start.isPending ? cluster ? 'Issuing ticket…' : 'Checking connection…' : cluster ? 'Issue ticket' : 'Start forward'}</button>
    {start.isError && <Notice title={cluster ? 'Ticket was not issued' : 'Forward did not start'} danger>{message(start.error)}</Notice>}
    {start.data && 'session' in start.data && start.data.session && <p className="small" role="status">Forward {start.data.session.state} on port {start.data.session.local_port}.</p>}
    {ticket && <div className="access-session" role="status"><div><p><strong>One-time pairing secret</strong></p><p className="small muted">Shown only now; claim it within two minutes.</p><p><code>{ticket.pairing_secret}</code></p><pre>{`piceli ui connect --server ${server} --ticket ${ticket.session.id} --kubeconfig /path/to/kubeconfig --context YOUR_CONTEXT --local-port ${local}`}</pre></div></div>}
  </section>;
}
