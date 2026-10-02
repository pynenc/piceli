import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { Badge, Failure, formatTime, Loading, Notice } from '../../components/State';
import '../control/operational-harbor.css';

export type ClusterStatus = {
  schema: string; state: string; cluster: string;
  nodes: { name: string; arch: string | null; roles: string[]; ready: boolean | null; mirror: { kind: string | null; state: string } | null }[];
  registry: { state: string; host: string | null; ready: boolean; pods: { name: string; node: string; phase: string; ready: boolean }[]; storage: { claim: string; phase: string; capacity: string; used_bytes: number | null } | null } | null;
  controller: { health: string; last_poll: string | null; poll_failures: number | null } | null;
  ui: { health: string } | null;
};

export function Cluster() {
  const query = useQuery({ queryKey: ['cluster-status'], queryFn: ({ signal }) => api.clusterStatus(signal).then(value => value as ClusterStatus), refetchInterval: 15000 });
  const status = query.data;
  return <div className="cluster-overview">
    <div className="heading detail-heading"><div><p className="eyebrow">Infrastructure</p><h1>Cluster</h1><p className="subtitle">Node placement, local registry, controller and UI health.</p></div><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh status</button></div>
    {query.isPending && <Loading text="Loading cluster status…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {status && <>
      <div className="statusbar" aria-label="Cluster state"><div className="statusitem"><span className="statuslabel">Cluster</span><strong>{status.cluster}</strong></div><div className="statusitem"><span className="statuslabel">State</span><Badge value={status.state} /></div><div className="statusitem"><span className="statuslabel">Nodes</span><strong>{status.nodes.length}</strong></div></div>
      <section className="panel control-card" aria-label="Cluster nodes"><div className="panelhead"><h2>Nodes</h2></div><div className="panelbody">
        {status.nodes.length === 0 ? <Notice title="No nodes reported">Node status is unavailable with this UI's current cluster read grant, or the cluster has no declared nodes.</Notice> : <ul className="cluster-nodes">{status.nodes.map(node => <li key={node.name}>
          <div className="cluster-node-name"><strong>{node.name}</strong><span className="small muted">{node.arch ?? 'Architecture unknown'}</span></div>
          <div><span className="state-label">Readiness</span><Badge value={node.ready === null ? 'unknown' : node.ready ? 'ready' : 'unavailable'} /></div>
          <div><span className="state-label">Roles</span><span>{node.roles.length ? node.roles.join(', ') : 'No Piceli roles'}</span></div>
          <div><span className="state-label">Mirror</span>{node.mirror ? <><Badge value={node.mirror.state} /><span className="small muted"> {node.mirror.kind ?? 'Runtime unknown'}</span>{node.mirror.state === 'needs-restart' && <p className="small">Restart k3s on this node to load the new mirror.</p>}</> : 'No registry configured'}</div>
        </li>)}</ul>}
      </div></section>
      <div className="cluster-services">
        <section className="panel control-card" aria-label="In-cluster registry"><div className="panelhead"><h2>In-cluster registry</h2><Badge value={status.registry?.state ?? 'not-installed'} /></div><div className="panelbody">
          {status.registry ? <><dl className="facts"><dt>Host</dt><dd><code>{status.registry.host ?? 'Unknown'}</code></dd><dt>Ready</dt><dd>{status.registry.ready ? 'Yes' : 'No'}</dd><dt>Storage</dt><dd>{status.registry.storage ? `${status.registry.storage.claim} · ${status.registry.storage.phase ?? 'Unknown'} · ${status.registry.storage.capacity ?? 'Size unknown'}` : 'Not reported'}</dd><dt>Used</dt><dd>{status.registry.storage?.used_bytes == null ? 'Not reported' : `${(status.registry.storage.used_bytes / (1024 ** 3)).toFixed(2)} GiB`}</dd></dl><h3>Pods</h3>{status.registry.pods.length ? <ul className="control-workloads">{status.registry.pods.map(pod => <li key={pod.name}><strong>{pod.name}</strong><span>{pod.node ?? 'Node unknown'} · {pod.phase ?? 'Unknown'} · {pod.ready ? 'ready' : 'not ready'}</span></li>)}</ul> : <p className="small muted">No registry pods reported.</p>}</> : <p className="small muted">No in-cluster registry is configured.</p>}
        </div></section>
        <section className="panel control-card" aria-label="Controller health"><div className="panelhead"><h2>Controller</h2><Badge value={status.controller?.health ?? 'unknown'} /></div><div className="panelbody"><dl className="facts"><dt>Last poll</dt><dd>{formatTime(status.controller?.last_poll)}</dd><dt>Poll failures</dt><dd>{status.controller?.poll_failures ?? 'Unknown'}</dd></dl></div></section>
        <section className="panel control-card" aria-label="UI health"><div className="panelhead"><h2>UI</h2><Badge value={status.ui?.health ?? 'unknown'} /></div><div className="panelbody"><p className="small muted">Health of the installed UI Deployment.</p></div></section>
      </div>
    </>}
  </div>;
}
