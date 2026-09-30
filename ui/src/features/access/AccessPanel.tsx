import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError } from '../../api/client';
import type { Resource } from '../../api/generated';
import { Badge, formatTime, Loading, Notice } from '../../components/State';

export function AccessPanel({ applicationId, resource }: { applicationId: string; resource: Resource }) {
  const client = useQueryClient();
  const [remotePort, setRemotePort] = useState(resource.ports?.[0] ?? 0);
  const [localPort, setLocalPort] = useState(resource.ports?.[0] ?? 0);
  const sessions = useQuery({ queryKey: ['access-sessions', applicationId], queryFn: ({ signal }) => api.accessSessions(applicationId, signal), refetchInterval: 3000 });
  const start = useMutation({ mutationFn: () => api.startAccess(applicationId, { resource_id: resource.id, resource_uid: resource.identity.uid ?? '', local_port: localPort, remote_port: remotePort }), retry: false, onSuccess: () => void client.invalidateQueries({ queryKey: ['access-sessions', applicationId] }) });
  const stop = useMutation({ mutationFn: (id: string) => api.stopAccess(applicationId, id), retry: false, onSuccess: () => void client.invalidateQueries({ queryKey: ['access-sessions', applicationId] }) });
  const relevant = sessions.data?.items.filter(item => item.resource.kind === resource.identity.kind && item.resource.name === resource.identity.name) ?? [];
  const valid = Boolean(resource.identity.uid && resource.ports?.includes(remotePort) && Number.isInteger(localPort) && localPort >= 1 && localPort <= 65535);
  return <section className="access-panel"><h4>Local connection</h4><p className="small muted">Piceli supervises a loopback forward on the host running this local UI. Ready means that owned listener passed a TCP probe. A port on a remote Piceli server is never presented as a port on your laptop.</p>
    {!resource.capabilities?.access?.allowed ? <Notice title="Access unavailable">{resource.capabilities?.access?.reason ?? 'No forwardable port was observed.'}</Notice> : <div className="access-form"><label>Target port<select value={remotePort} onChange={event => setRemotePort(Number(event.target.value))}>{resource.ports?.map(port => <option key={port} value={port}>{port}</option>)}</select></label><label>Local port<input type="number" min={1} max={65535} value={localPort} onChange={event => setLocalPort(Number(event.target.value))} /></label><button className="primary" disabled={!valid || start.isPending} onClick={() => start.mutate()}>{start.isPending ? 'Checking connection…' : 'Start local connection'}</button></div>}
    {start.isError && <Notice title="Connection did not start" danger>{start.error instanceof ApiError ? start.error.detail.message : 'The local connection failed. Refresh this resource and try again.'}</Notice>}
    {stop.isError && <Notice title="Connection did not stop" danger>{stop.error instanceof ApiError ? stop.error.detail.message : 'Refresh the session and try again.'}</Notice>}
    {sessions.isPending && <Loading text="Loading connection sessions…" />}{sessions.isError && <Notice title="Connection status unavailable">Refresh the resource to retry.</Notice>}
    {relevant.map(session => <div className="access-session" key={session.id}><div><Badge value={session.state} /><p className="small">{session.binding_location === 'server' ? 'On this Piceli host' : session.binding_location === 'local_client' ? 'On the local client' : 'Through a gateway'} · {session.local_port} → {session.remote_port}</p>{session.endpoint && <p><code>{session.endpoint}</code></p>}<p className="small muted">Expires {formatTime(session.expires_at)}{session.resource.uid !== resource.identity.uid ? ' · Selected resource was replaced' : ''}</p></div>{['ready', 'connecting', 'pending'].includes(session.state) && <button disabled={stop.isPending} onClick={() => stop.mutate(session.id)}>Stop connection</button>}</div>)}
  </section>;
}
