import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError, basePath } from '../../api/client';
import type { AccessSession, Resource } from '../../api/generated';
import { Badge, formatTime, Loading, Notice } from '../../components/State';

type Props = { applicationId: string; resource: Resource };

function ErrorNotice({ title, error }: { title: string; error: Error }) {
  return <Notice title={title} danger>{error instanceof ApiError ? error.detail.message : 'Refresh this resource and try again.'}</Notice>;
}

function SessionRows({ items, resource, stop, stopping }: { items: AccessSession[]; resource: Resource; stop: (id: string) => void; stopping: boolean }) {
  const relevant = items.filter(item => item.resource.kind === resource.identity.kind && item.resource.name === resource.identity.name);
  return <>{relevant.map(session => <div className="access-session" key={session.id}><div><Badge value={session.state} /><p className="small">{session.binding_location === 'server' ? 'On this Piceli host' : session.binding_location === 'local_client' ? 'On your local client (client-reported)' : 'Through a gateway'} · {session.local_port ?? '—'} → {session.remote_port}</p>{session.endpoint && <p><code>{session.endpoint}</code></p>}<p className="small muted">Expires {formatTime(session.expires_at)}{session.resource.uid !== resource.identity.uid ? ' · Selected resource was replaced' : ''}</p></div>{['ready', 'connecting', 'pending'].includes(session.state) && <button disabled={stopping} onClick={() => stop(session.id)}>Stop connection</button>}</div>)}</>;
}

export function AccessPanel(props: Props) {
  const capabilities = useQuery({ queryKey: ['capabilities'], queryFn: ({ signal }) => api.capabilities(signal) });
  if (capabilities.isPending) return <Loading text="Checking connection mode…" />;
  if (capabilities.isError) return <Notice title="Connection mode unavailable">Refresh the resource to retry.</Notice>;
  return capabilities.data?.mode === 'cluster' ? <RemoteAccessPanel {...props} /> : <LocalAccessPanel {...props} />;
}

function LocalAccessPanel({ applicationId, resource }: Props) {
  const client = useQueryClient();
  const [remotePort, setRemotePort] = useState(resource.ports?.[0] ?? 0);
  const [localPort, setLocalPort] = useState(resource.ports?.[0] ?? 0);
  const sessions = useQuery({ queryKey: ['access-sessions', applicationId], queryFn: ({ signal }) => api.accessSessions(applicationId, signal), refetchInterval: 3000 });
  const start = useMutation({ mutationFn: () => api.startAccess(applicationId, { resource_id: resource.id, resource_uid: resource.identity.uid ?? '', local_port: localPort, remote_port: remotePort }), retry: false, onSuccess: () => void client.invalidateQueries({ queryKey: ['access-sessions', applicationId] }) });
  const stop = useMutation({ mutationFn: (id: string) => api.stopAccess(applicationId, id), retry: false, onSuccess: () => void client.invalidateQueries({ queryKey: ['access-sessions', applicationId] }) });
  const valid = Boolean(resource.identity.uid && resource.ports?.includes(remotePort) && Number.isInteger(localPort) && localPort >= 1 && localPort <= 65535);
  return <section className="access-panel"><h4>Local connection</h4><p className="small muted">Piceli supervises a loopback forward on the host running this local UI. Ready means that owned listener passed a TCP probe. A port on a remote Piceli server is never presented as a port on your laptop.</p>
    {!resource.capabilities?.access?.allowed ? <Notice title="Access unavailable">{resource.capabilities?.access?.reason ?? 'No forwardable port was observed.'}</Notice> : <div className="access-form"><label>Target port<select value={remotePort} onChange={event => setRemotePort(Number(event.target.value))}>{resource.ports?.map(port => <option key={port} value={port}>{port}</option>)}</select></label><label>Local port<input type="number" min={1} max={65535} value={localPort} onChange={event => setLocalPort(Number(event.target.value))} /></label><button className="primary" disabled={!valid || start.isPending} onClick={() => start.mutate()}>{start.isPending ? 'Checking connection…' : 'Start local connection'}</button></div>}
    {start.isError && <ErrorNotice title="Connection did not start" error={start.error} />}{stop.isError && <ErrorNotice title="Connection did not stop" error={stop.error} />}
    {sessions.isPending && <Loading text="Loading connection sessions…" />}{sessions.isError && <Notice title="Connection status unavailable">Refresh the resource to retry.</Notice>}
    <SessionRows items={sessions.data?.items ?? []} resource={resource} stop={id => stop.mutate(id)} stopping={stop.isPending} />
  </section>;
}

function RemoteAccessPanel({ applicationId, resource }: Props) {
  const client = useQueryClient();
  const [remotePort, setRemotePort] = useState(resource.ports?.[0] ?? 0);
  const [localPort, setLocalPort] = useState((resource.ports?.[0] ?? 0) >= 1024 ? resource.ports![0] : 8080);
  const sessions = useQuery({ queryKey: ['remote-access', applicationId], queryFn: ({ signal }) => api.remoteAccessSessions(applicationId, signal), refetchInterval: 3000 });
  const issue = useMutation({ mutationFn: () => api.issueRemoteAccess(applicationId, { resource_id: resource.id, resource_uid: resource.identity.uid ?? '', remote_port: remotePort }), retry: false, onSuccess: () => void client.invalidateQueries({ queryKey: ['remote-access', applicationId] }) });
  const stop = useMutation({ mutationFn: (id: string) => api.stopRemoteAccess(applicationId, id), retry: false, onSuccess: (_session, id) => { if (issue.data?.session.id === id) issue.reset(); void client.invalidateQueries({ queryKey: ['remote-access', applicationId] }); } });
  const valid = Boolean(resource.identity.uid && resource.ports?.includes(remotePort) && Number.isInteger(localPort) && localPort >= 1 && localPort <= 65535);
  const server = `${window.location.origin}${basePath()}`;
  const ticket = issue.data;
  const currentTicket = sessions.data?.items.find(item => item.id === ticket?.session.id);
  const claimable = ticket && (!currentTicket || currentTicket.state === 'pending');
  return <section className="access-panel"><h4>Connect from your machine</h4><p className="small muted">This Piceli service runs remotely. Issue a short-lived ticket here, then start the connection on your own machine with an explicit kubeconfig. Pending means no local port is open yet; ready is reported by your client after it probes its own loopback port.</p>
    {!resource.capabilities?.access?.allowed ? <Notice title="Access unavailable">{resource.capabilities?.access?.reason ?? 'No forwardable port was observed.'}</Notice> : <div className="access-form"><label>Target port<select value={remotePort} onChange={event => setRemotePort(Number(event.target.value))}>{resource.ports?.map(port => <option key={port} value={port}>{port}</option>)}</select></label><label>Port on your machine<input type="number" min={1} max={65535} value={localPort} onChange={event => setLocalPort(Number(event.target.value))} /></label><button className="primary" disabled={!valid || issue.isPending} onClick={() => issue.mutate()}>{issue.isPending ? 'Issuing ticket…' : 'Issue connection ticket'}</button></div>}
    {issue.isError && <ErrorNotice title="Ticket was not issued" error={issue.error} />}{stop.isError && <ErrorNotice title="Connection did not stop" error={stop.error} />}
    {claimable && <div className="access-session" role="status"><div><p><strong>One-time pairing secret</strong></p><p className="small muted">Keep this private and claim it within two minutes. It is shown only now; if you leave this page, issue a new ticket.</p><p><code>{ticket.pairing_secret}</code></p><p><strong>Ticket ID</strong> <code>{ticket.session.id}</code></p><p className="small">On your machine, run the command below. It prompts for the pairing secret without echo.</p><pre>{`piceli ui connect --server ${server} --ticket ${ticket.session.id} --kubeconfig /path/to/kubeconfig --context YOUR_CONTEXT --local-port ${localPort}`}</pre></div></div>}
    {ticket && !claimable && <p className="small muted">This ticket was claimed or ended. Its pairing secret can no longer be used.</p>}
    {sessions.isPending && <Loading text="Loading connection tickets…" />}{sessions.isError && <Notice title="Connection status unavailable">Refresh the resource to retry.</Notice>}
    <SessionRows items={sessions.data?.items ?? []} resource={resource} stop={id => stop.mutate(id)} stopping={stop.isPending} />
  </section>;
}
