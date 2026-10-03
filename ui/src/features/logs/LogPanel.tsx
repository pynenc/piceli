import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useVirtualizer } from '@tanstack/react-virtual';
import { Link } from 'react-router-dom';
import { api, logsPath } from '../../api/client';
import type { Resource } from '../../api/generated';
import { Failure, FreshnessNotice, Loading, Notice } from '../../components/State';
import { emptyLogBuffer, mergeLogBatch, type LogBuffer } from './buffer';

export function LogPanel({ applicationId, resource }: { applicationId: string; resource: Resource }) {
  const [selectedUid, setSelectedUid] = useState<string | null>(null);
  const [selectedContainer, setSelectedContainer] = useState<string | null>(null);
  const [previous, setPrevious] = useState(false);
  const [tailLines, setTailLines] = useState(2000);
  const [buffer, setBuffer] = useState<LogBuffer>(() => emptyLogBuffer(''));
  const scroll = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const sources = useQuery({
    queryKey: ['log-sources', applicationId, resource.id, resource.identity.uid],
    queryFn: ({ signal }) => api.logSources(applicationId, resource.id, resource.identity.uid ?? '', signal),
    refetchInterval: 5000,
  });
  const items = sources.data?.items ?? [];
  const pod = items.find(item => item.pod.uid === selectedUid) ?? items.at(-1);
  const container = pod?.containers.includes(selectedContainer ?? '') ? selectedContainer! : pod?.containers[0];
  const replaced = Boolean(selectedUid && items.length && pod?.pod.uid !== selectedUid);
  const selectionKey = pod?.pod.uid && container ? `${resource.id}/${resource.identity.uid}/${pod.pod.uid}/${container}/${previous}` : '';
  const logs = useQuery({
    queryKey: ['logs', applicationId, selectionKey, tailLines],
    queryFn: ({ signal }) => api.logs(applicationId, resource.id, resource.identity.uid ?? '', pod!.pod.name, pod!.pod.uid!, container!, previous, tailLines, signal),
    enabled: Boolean(selectionKey),
    refetchInterval: 1000,
    retry: false,
  });
  useEffect(() => setBuffer(emptyLogBuffer(selectionKey)), [selectionKey]);
  useEffect(() => {
    if (selectionKey && logs.data) setBuffer(current => mergeLogBatch(current, selectionKey, logs.data));
  }, [selectionKey, logs.data]);
  const virtual = useVirtualizer({ count: buffer.lines.length, getScrollElement: () => scroll.current, estimateSize: () => 20, overscan: 12 });
  useEffect(() => {
    if (follow.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [buffer.lines.length]);

  return <section className="log-panel">
    <div className="log-panel-head"><h4>Container logs</h4><Link to={logsPath({ scope: applicationId, ...(resource.identity.kind === 'Pod' ? { pod: resource.identity.name } : { workload: `${resource.identity.kind}/${resource.identity.name}` }) })}>Open in Logs →</Link></div>
    <p className="small muted">Pod and container choices come from this workload's observed scope. A missing pod, an unreadable log, or a missed interval remains visible. The bounded read refreshes about once a second.</p>
    {sources.isPending && <Loading text="Finding workload pods…" />}
    {sources.isError && <Failure error={sources.error} retry={() => void sources.refetch()} />}
    {sources.data && <>
      <FreshnessNotice freshness={sources.data.freshness} disconnected={sources.isError} />
      {(sources.data.partial?.length ?? 0) > 0 && <Notice title="Partial pod observation">Some pod or owner information could not be read. Log choices may be incomplete.</Notice>}
      {items.length === 0 && <Notice title="No matching pods">No pod currently belongs to this selected workload. Refresh after it starts or check observation permissions.</Notice>}
    </>}
    {replaced && <Notice title="Pod changed">The previously selected pod is no longer present. Showing the newest observed pod for this workload.</Notice>}
    {pod && <div className="log-controls">
      <label>Pod<select value={pod.pod.uid ?? ''} onChange={event => { setSelectedUid(event.target.value); setSelectedContainer(null); setPrevious(false); follow.current = true; }}>{items.map(item => <option key={item.pod.uid ?? item.pod.name} value={item.pod.uid ?? ''}>{item.pod.name} · {item.phase ?? 'unknown'}</option>)}</select></label>
      <label>Container<select value={container ?? ''} onChange={event => { setSelectedContainer(event.target.value); setPrevious(false); follow.current = true; }}>{pod.containers.map(name => <option key={name} value={name}>{name}</option>)}</select></label>
      <label>Instance<select value={previous ? 'previous' : 'current'} onChange={event => { setPrevious(event.target.value === 'previous'); follow.current = true; }}><option value="current">Current</option><option value="previous">Previous</option></select></label>
      <label>Lines per read<select value={tailLines} onChange={event => setTailLines(Number(event.target.value))}><option value={200}>200</option><option value={2000}>2,000</option><option value={5000}>5,000</option></select></label>
      <button disabled={logs.isFetching} onClick={() => void logs.refetch()}>Refresh logs</button>
    </div>}
    {pod && !container && <Notice title="No container">The selected pod has no observed container.</Notice>}
    {logs.isPending && pod && container && <Loading text="Reading container logs…" />}
    {logs.isError && <Failure error={logs.error} retry={() => void logs.refetch()} />}
    {buffer.gap && <Notice title="Log history has a gap">A bounded read missed an interval or older lines were discarded. The displayed lines remain in order; inspect the source if complete history is required.</Notice>}
    {buffer.dropped > 0 && <p className="small muted">{buffer.dropped.toLocaleString()} older lines were removed from this browser's buffer.</p>}
    {logs.data && buffer.lines.length === 0 && <p className="small muted">No log lines returned for this container instance.</p>}
    {selectionKey && <div className="log-output" ref={scroll} tabIndex={0} role="region" aria-label="Container log lines" onScroll={event => { const node = event.currentTarget; follow.current = node.scrollHeight - node.scrollTop - node.clientHeight < 40; }}>
      <div style={{ height: virtual.getTotalSize(), position: 'relative' }}>{virtual.getVirtualItems().map(item => <div className="log-line" key={item.key} style={{ position: 'absolute', top: 0, left: 0, transform: `translateY(${item.start}px)`, height: item.size }}>{buffer.lines[item.index] || '\u00a0'}</div>)}</div>
    </div>}
  </section>;
}
