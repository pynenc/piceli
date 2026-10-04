import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { Badge, Failure, formatTime, Loading, Notice } from '../../components/State';
import '../control/operational-harbor.css';

export type MachineServer = {
  name: string; provider: string; type: string; location: string; image: string;
  state: string; id: string | null; status: string | null; ipv4: string | null; ipv6: string | null; fixed_ipv4: boolean;
  firewall: string | null; host_keys: { type: string; fingerprint: string }[];
  install: { declared: boolean; state: string | null; at?: string; exit_code?: number | null };
  cluster: { name: string; api: string; profile: string; registered: boolean; registered_at: string | null; nodes: { nodes: number; ready: number } | null } | null;
  monthly_net: number | null;
};
export type MachinesStatus = {
  schema: string; name: string; state: string; ref: string; updated_at: string | null; tofu: string | null;
  backend: { kind: string; address?: string } | null;
  servers: MachineServer[];
  records: { key: string; type: string; state: string; servers: string[]; value: string[] | null; ttl: number }[];
  resources: number;
  estimate: { currency: string; monthly_net: number; complete: boolean } | null;
  commands: { plan: string; status: string };
};

const money = (value: number | null | undefined, currency = 'EUR') => value == null ? 'Unknown' : `${value.toFixed(2)} ${currency}`;

export function Machines() {
  const query = useQuery({ queryKey: ['machines'], queryFn: ({ signal }) => api.machines(signal).then(value => value as unknown as MachinesStatus), refetchInterval: 30000 });
  const status = query.data;
  const currency = status?.estimate?.currency ?? 'EUR';
  return <div className="cluster-overview">
    <div className="heading detail-heading"><div><p className="eyebrow">Infrastructure</p><h1>Machines</h1><p className="subtitle">Servers, addresses, OS install and cluster registration, as the last apply recorded them. Plans and applies run on the command line, with approval.</p></div><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh</button></div>
    {query.isPending && <Loading text="Loading machines…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {status && <>
      <div className="statusbar" aria-label="Infrastructure state">
        <div className="statusitem"><span className="statuslabel">Infrastructure</span><strong>{status.name}</strong></div>
        <div className="statusitem"><span className="statuslabel">State</span><Badge value={status.state} /></div>
        <div className="statusitem"><span className="statuslabel">Servers</span><strong>{status.servers.length}</strong></div>
        <div className="statusitem"><span className="statuslabel">Estimate</span><strong>{status.estimate ? `${money(status.estimate.monthly_net, currency)} / month${status.estimate.complete ? '' : ' (partial)'}` : 'Not available'}</strong></div>
      </div>
      <section className="panel control-card" aria-label="Servers"><div className="panelhead"><h2>Servers</h2><span className="small muted">Updated {formatTime(status.updated_at)}</span></div><div className="panelbody">
        {status.servers.length === 0 ? <Notice title="No servers declared">This infrastructure declares no servers.</Notice> : <ul className="cluster-nodes">{status.servers.map(server => <li key={server.name}>
          <div className="cluster-node-name"><strong>{server.name}</strong><span className="small muted">{server.provider} · {server.type} · {server.location}</span></div>
          <div><span className="state-label">State</span><Badge value={server.state} />{server.status && <span className="small muted"> {server.status}</span>}</div>
          <div><span className="state-label">Addresses</span><span>{server.ipv4 ?? 'No IPv4'}{server.fixed_ipv4 ? ' (fixed)' : ''}{server.ipv6 ? ` · ${server.ipv6}` : ''}</span></div>
          <div><span className="state-label">OS install</span>{server.install.declared ? <Badge value={server.install.state ?? 'not-run'} /> : <span className="muted">No hook</span>}</div>
          <div><span className="state-label">Cluster</span>{server.cluster ? <><Badge value={server.cluster.registered ? 'registered' : 'not-registered'} /><span className="small muted"> {server.cluster.name}{server.cluster.nodes ? ` · ${server.cluster.nodes.ready}/${server.cluster.nodes.nodes} nodes ready` : ''}</span></> : <span className="muted">None declared</span>}</div>
          <div><span className="state-label">Cost</span><span>{server.monthly_net == null ? 'Unknown' : `${money(server.monthly_net, currency)} / month`}</span></div>
          {server.host_keys.length > 0 && <div><span className="state-label">Host keys</span><span className="small">{server.host_keys.map(key => <code key={key.fingerprint}>{key.type} {key.fingerprint} </code>)}</span></div>}
        </li>)}</ul>}
      </div></section>
      <section className="panel control-card" aria-label="DNS records"><div className="panelhead"><h2>DNS records</h2></div><div className="panelbody">
        {status.records.length === 0 ? <p className="small muted">No DNS records declared.</p> : <ul className="control-workloads">{status.records.map(record => <li key={record.key}><strong>{record.key}</strong><span><Badge value={record.state} /> {record.servers.length ? `→ ${record.servers.join(', ')}` : (record.value ?? []).join(', ')} · ttl {record.ttl}s</span></li>)}</ul>}
      </div></section>
      <section className="panel control-card" aria-label="Commands"><div className="panelhead"><h2>Change it</h2></div><div className="panelbody"><p className="small">Review a plan and approve its hash from the command line:</p><pre><code>{status.commands.plan}</code></pre><p className="small muted">State backend: {status.backend?.kind ?? 'local'}{status.tofu ? ` · OpenTofu ${status.tofu}` : ''} · {status.resources} resources</p></div></section>
    </>}
  </div>;
}
