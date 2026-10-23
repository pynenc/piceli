// Every GitOps environment and NixHost in one table: the document of
// `piceli targets` (piceli.targets.v1), built by the same server function.
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { Badge, Failure, formatTime, Loading, Notice } from '../../components/State';
import './composition-harbor.css';

type Pinned = { source?: string | null; input?: string | null; commit?: string | null; commits?: Record<string, string>; environment?: string | null; verified_at?: string | null };
type Release = { state?: string | null; tag?: string | null; commit?: string | null; at?: string | null } | null;
export type Target = {
  kind: string; name: string; where?: string | null; state: string; reason?: string | null; detail?: string | null; as_of?: string | null;
  revision?: { commit?: string | null; tag?: string | null; commits?: Record<string, string> } | null; pinned?: Pinned | null;
  checks: { state: string; at?: string | null; passed?: number | null; total?: number | null };
  facts: Record<string, string>; release?: Release;
};
export type TargetsDocument = {
  configured: boolean; state: string; hosts_read: boolean; generated_at: string; targets: Target[];
  controller: { read: boolean; health?: string | null; as_of?: string | null; reason?: string | null };
};

const short = (sha?: string | null) => (sha ? sha.slice(0, 7) : '');
const commits = (map?: Record<string, string>) => Object.entries(map ?? {}).map(([source, sha]) => `${source}@${short(sha)}`).join(', ');

function Revision({ target }: { target: Target }) {
  const revision = target.revision;
  if (!revision) return <span className="muted">Not reported</span>;
  if (revision.commit) return <>{revision.tag && <strong>{revision.tag} </strong>}<code title={revision.commit}>{short(revision.commit)}</code></>;
  return <code>{commits(revision.commits) || 'Not reported'}</code>;
}

function PinnedCell({ pinned }: { pinned?: Pinned | null }) {
  if (!pinned) return <span className="muted">No gate</span>;
  if (!pinned.commit) return <code title="No product source named: every source's commit">{commits(pinned.commits) || 'Not reported'}</code>;
  const label = pinned.source ?? pinned.input;
  return <><code title={pinned.commit}>{label ? `${label}@` : ''}{short(pinned.commit)}</code>{pinned.environment && <small>verified in {pinned.environment}{pinned.verified_at ? ` · ${formatTime(pinned.verified_at)}` : ''}</small>}</>;
}

function Row({ target }: { target: Target }) {
  const checks = target.checks;
  return <tr aria-label={`Target ${target.name}${target.where ? ` ${target.where}` : ''}`}>
    <th scope="row"><strong>{target.name}</strong><small>{target.kind === 'nix-host' ? 'NixHost' : 'Environment'} · {target.where ?? 'location not reported'}</small></th>
    <td><Revision target={target} />{target.release && <small>Release {target.release.state ?? 'unknown'}{target.release.at ? ` · ${formatTime(target.release.at)}` : ''}</small>}</td>
    <td><PinnedCell pinned={target.pinned} /></td>
    <td><Badge value={checks.state} />{checks.total ? <small>{checks.passed}/{checks.total} · {formatTime(checks.at)}</small> : null}</td>
    <td><Badge value={target.state} />{target.reason && <small>{target.reason}</small>}</td>
    <td>{formatTime(target.as_of)}<small>from {target.facts.state === 'host-agent' ? 'the host agent' : 'the controller'}</small></td>
  </tr>;
}

export function Targets() {
  const query = useQuery({ queryKey: ['composition-targets'], queryFn: ({ signal }) => api.compositionTargets(signal).then(value => value as TargetsDocument), refetchInterval: 15000 });
  const data = query.data;
  const hosts = data?.targets.some(target => target.kind === 'nix-host');
  return <div className="composition-targets">
    <div className="heading"><div><h1>Targets</h1><p className="subtitle">Every environment and NixHost: its revision, the product commit it pins, its checks and its state.</p></div><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh targets</button></div>
    {query.isPending && <Loading text="Loading targets…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {data && !data.configured && <Notice title="No GitOps controller status yet">Enable GitOps for the composition with <code>piceli gitops enable MODULE</code>.</Notice>}
    {data && hosts && !data.hosts_read && <Notice title="Hosts are not reached from this UI">It cannot SSH to hosts: each NixHost shows its release as the controller published it (tag, pinned product commit, when). <code>piceli targets --composition MODULE --infra MODULE</code> reads each host's own state.</Notice>}
    {data?.configured && <section className="panel" aria-label="Targets"><div className="composition-matrix-scroll"><table className="composition-version-matrix">
      <thead><tr><th scope="col">Target</th><th scope="col">Revision</th><th scope="col">Pinned product</th><th scope="col">Checks</th><th scope="col">State</th><th scope="col">As of</th></tr></thead>
      <tbody>{data.targets.map(target => <Row key={`${target.kind}/${target.name}/${target.where ?? ''}`} target={target} />)}</tbody>
    </table></div></section>}
  </div>;
}
