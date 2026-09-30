import { useEffect, useRef } from 'react';
import { useInfiniteQuery, useQuery, useQueryClient, type InfiniteData } from '@tanstack/react-query';
import { useVirtualizer } from '@tanstack/react-virtual';
import * as Dialog from '@radix-ui/react-dialog';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import type { Application, Resource, ResourcePage } from '../api/generated';
import { useUrlText } from './useUrlText';
import { Badge, Failure, FreshnessNotice, Loading, Notice } from '../components/State';
import { AccessPanel } from '../features/access/AccessPanel';
import { LogPanel } from '../features/logs/LogPanel';
import { useObservationEvents } from '../features/observation/useObservationEvents';
import { orderByOwner, type RelatedResource } from '../features/observation/topology';

export function Resources({ application }: { application: Application }) {
  const [params, setParams] = useSearchParams();
  const client = useQueryClient();
  const refreshed = useRef(0);
  const pinned = useRef<string | null>(null);
  const [filter, setFilter] = useUrlText('filter');
  const selected = params.get('resource');
  const panel = params.get('panel') ?? 'summary';
  const trigger = useRef<HTMLElement | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const query = useInfiniteQuery({ queryKey: ['resources', application.id], initialPageParam: null as string | null, queryFn: ({ signal, pageParam }) => api.resources(application.id, pageParam, signal), getNextPageParam: page => page.next_page ?? undefined, structuralSharing: retainUnavailableInventory });
  useEffect(() => {
    if (!query.dataUpdatedAt || refreshed.current === query.dataUpdatedAt) return;
    refreshed.current = query.dataUpdatedAt;
    // Observation updates the service's application state. Read that authority
    // again rather than deriving application freshness or target identity here.
    void client.invalidateQueries({ queryKey: ['application', application.id] });
    void client.invalidateQueries({ queryKey: ['applications'] });
    if (pinned.current !== application.id && query.data?.pages.some(page => page.freshness.state === 'connected')) {
      pinned.current = application.id;
      void client.invalidateQueries({ queryKey: ['capabilities'] });
    }
  }, [client, application.id, query.dataUpdatedAt, query.data]);
  const pages = query.data?.pages;
  const observation = useObservationEvents(application.id, pages?.[0]?.event_cursor);
  const resources = pages?.flatMap(page => page.items) ?? [];
  const filtered = resources.filter(r => `${r.identity.name} ${r.identity.kind} ${r.identity.namespace}`.toLowerCase().includes(filter.toLowerCase()));
  const view = params.get('view') === 'relationships' ? 'relationships' : 'table';
  const rows: RelatedResource[] = view === 'relationships' ? orderByOwner(filtered) : filtered.map(resource => ({ resource, depth: 0 }));
  const partial = pages?.flatMap(page => page.partial ?? []) ?? [];
  const change = (key: string, value: string) => { const next = new URLSearchParams(params); if (value) next.set(key, value); else next.delete(key); setParams(next, { replace: true }); };
  const close = () => { const next = new URLSearchParams(params); next.delete('resource'); next.delete('panel'); setParams(next, { replace: true }); };
  const select = (resource: Resource) => { trigger.current = document.activeElement as HTMLElement; const next = new URLSearchParams(params); next.set('resource', resource.id); next.set('panel', 'summary'); setParams(next); };
  return <><section className="panel resource-panel"><div className="panelhead"><div><h2 ref={heading} tabIndex={-1}>Resources <span className="count">{resources.length}{query.hasNextPage ? '+' : ''}</span></h2><p className="small muted">Presence, health and desired/live state are separate observations.</p></div><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh resources</button></div>
    <div className="resource-views" role="group" aria-label="Resource view"><button aria-pressed={view === 'table'} onClick={() => change('view', '')}>Table</button><button aria-pressed={view === 'relationships'} onClick={() => change('view', 'relationships')}>Relationships</button></div>
    <div className="resource-filter"><label htmlFor="resource-filter">Filter resources</label><input id="resource-filter" type="search" placeholder="Name, kind or namespace…" value={filter} onChange={e => setFilter(e.target.value)} /></div>
    {query.isPending && <Loading />}{query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {pages && <><div className="resource-notices">{pages.map((p, i) => <FreshnessNotice key={i} freshness={p.freshness} disconnected={query.isError} />)}{observation.gap && <Notice title="Observation history expired">Some resource changes were missed. The current scope was fetched again.</Notice>}{!observation.connected && <Notice title="Live observation disconnected">The resource list is refreshing while the observation stream reconnects.</Notice>}{partial.length > 0 && <Notice title="Partial observation"><p>Some resources could not be read. The resources below are the visible portion of this scope.</p><ul>{partial.map((e, i) => <li key={i}>{e.scope}: {e.code}</li>)}</ul></Notice>}</div>
      {filtered.length ? <ResourceList rows={rows} relationships={view === 'relationships'} onSelect={select} /> : <div className="empty"><h3>{filter ? 'No matching resources' : partial.length ? 'No resources visible in this partial observation' : 'No resources observed'}</h3><p>{filter ? 'Try another name, kind or namespace.' : partial.length ? 'Resolve the observation errors to see the complete scope.' : 'This target scope currently contains no observed resources.'}</p></div>}
      {query.hasNextPage && <button className="load-more" onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>Load more resources</button>}
    </>}
  </section><Dialog.Root open={Boolean(selected)} onOpenChange={open => { if (!open) close(); }}><Dialog.Portal><Dialog.Overlay className="dialog-overlay" /><Dialog.Content className="inspector" onCloseAutoFocus={event => { event.preventDefault(); if (trigger.current?.isConnected) trigger.current.focus(); else heading.current?.focus(); }}>
    <div className="inspector-heading"><div><p className="eyebrow">Resource inspector</p><Dialog.Title>Resource details</Dialog.Title></div><Dialog.Close aria-label="Close resource inspector">✕</Dialog.Close></div><Dialog.Description className="sr-only">Inspect the selected resource in its application and target context.</Dialog.Description>
    {selected && <Inspector resourceId={selected} panel={panel} setPanel={value => change('panel', value)} application={application} freshness={pages?.[0]?.freshness ?? application.freshness} />}
  </Dialog.Content></Dialog.Portal></Dialog.Root></>;
}
function ResourceList({ rows, relationships, onSelect }: { rows: RelatedResource[]; relationships: boolean; onSelect: (r: Resource) => void }) {
  const scroll = useRef<HTMLDivElement>(null);
  const virtual = useVirtualizer({ count: rows.length, getScrollElement: () => scroll.current, estimateSize: () => 150, overscan: 8, getItemKey: index => rows[index].resource.id });
  const large = rows.length > 100;
  return <div className="resource-scroll" ref={scroll} role="region" aria-label="Resource list" tabIndex={large ? 0 : undefined}>
    {large && <p className="sr-only">Use Up and Down arrow keys within the list to move between resources.</p>}
    <ul className="resource-list" style={large ? { height: virtual.getTotalSize(), position: 'relative' } : undefined}>
      {(large ? virtual.getVirtualItems().map(v => ({ index: v.index, start: v.start })) : rows.map((_, index) => ({ index, start: 0 }))).map(({ index, start }) => {
        const r = rows[index].resource;
        const depth = rows[index].depth;
        return <li key={r.id} data-index={index} ref={large ? virtual.measureElement : undefined} style={large ? { position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${start}px)` } : undefined}>
          <button className="resource-row" style={relationships ? { paddingInlineStart: 20 + Math.min(depth, 5) * 16 } : undefined} aria-label={`Inspect ${r.identity.kind} ${r.identity.name} in ${r.identity.namespace || "cluster scope"}`} data-resource-index={index} onClick={() => onSelect(r)} onKeyDown={event => {
            if (!large || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : Math.min(rows.length - 1, Math.max(0, index + (event.key === 'ArrowDown' ? 1 : -1)));
            virtual.scrollToIndex(next);
            requestAnimationFrame(() => scroll.current?.querySelector<HTMLButtonElement>(`[data-resource-index="${next}"]`)?.focus());
          }}><span className="resource-name"><span className="kind">{r.identity.kind}</span><strong>{r.identity.name}</strong><span className="small muted">{r.identity.namespace || 'Cluster scope'} · {r.ownership}</span>{relationships && <span className="small muted">{depth ? `Owner level ${depth}` : r.owner_uids?.length ? 'Owner outside observed inventory' : 'Top level'}</span>}</span><span className="resource-states"><span><span className="state-label">Presence</span><Badge value={r.presence} /></span><span><span className="state-label">Health</span><Badge value={r.health ?? 'unknown'} /></span><span><span className="state-label">Desired / live</span><Badge value={r.relation ?? 'unknown'} /></span></span><span aria-hidden="true" className="chevron">›</span></button>
        </li>;
      })}
    </ul>
  </div>;
}
function Inspector({ resourceId, panel, setPanel, application, freshness }: { resourceId: string; panel: string; setPanel: (value: string) => void; application: Application; freshness: Application['freshness'] }) {
  const query = useQuery({ queryKey: ['resource', application.id, resourceId], queryFn: ({ signal }) => api.resource(application.id, resourceId, signal) });
  if (!query.data) return query.isError ? <div className="inspector-body"><Failure error={query.error} retry={() => void query.refetch()} /></div> : <Loading text="Loading resource details…" />;
  const r = query.data;
  const tabs = ['summary', ...(r.capabilities?.conditions?.allowed ? ['conditions'] : []), ...(r.capabilities?.manifest?.allowed ? ['manifest'] : []), ...(r.capabilities?.logs?.allowed ? ['logs'] : []), ...(r.capabilities?.access?.allowed ? ['access'] : [])];
  const active = tabs.includes(panel) ? panel : 'summary';
  return <div className="inspector-body"><h3 className="resource-title">{r.identity.name}</h3><p className="subtitle">{r.identity.kind} · {application.target.name} / {r.identity.namespace || 'Cluster scope'}</p><FreshnessNotice freshness={freshness} disconnected={query.isError} />
    <dl className="facts identity"><dt>Target ID</dt><dd>{r.identity.target_id}</dd><dt>API / kind</dt><dd>{r.identity.api_version} / {r.identity.kind}</dd><dt>Namespace</dt><dd>{r.identity.namespace || 'Cluster scope'}</dd><dt>UID</dt><dd><code>{r.identity.uid ?? 'Not observed'}</code></dd><dt>Ownership</dt><dd>{r.ownership}</dd></dl>
    <nav className="tabs" aria-label="Resource panels">{tabs.map(t => <button key={t} aria-current={active === t ? 'page' : undefined} className={active === t ? 'active' : ''} onClick={() => setPanel(t)}>{t[0].toUpperCase() + t.slice(1)}</button>)}</nav>
    {active === 'summary' && <><dl className="facts"><dt>Presence</dt><dd><Badge value={r.presence} /></dd><dt>Health</dt><dd><Badge value={r.health ?? 'unknown'} /></dd><dt>Desired / live</dt><dd><Badge value={r.relation ?? 'unknown'} /></dd><dt>Phase</dt><dd>{r.phase || 'Not reported'}</dd><dt>Resource version</dt><dd>{r.resource_version || 'Not observed'}</dd></dl><h4>Images</h4>{r.images?.length ? <ul className="image-list">{r.images.map(image => <li key={image}><code>{image}</code></li>)}</ul> : <p className="muted small">No image information reported.</p>}<h4>Owner UIDs</h4>{r.owner_uids?.length ? <ul>{r.owner_uids.map(uid => <li key={uid}><code>{uid}</code></li>)}</ul> : <p className="muted small">No ownership references reported.</p>}</>}
    {active === 'conditions' && (r.conditions?.length ? <pre aria-label="Resource conditions">{JSON.stringify(r.conditions, null, 2)}</pre> : <p>No conditions reported.</p>)}
    {active === 'manifest' && (r.manifest ? <><p className="small muted">Sensitive fields are redacted by the service.</p><pre aria-label="Resource manifest">{JSON.stringify(r.manifest, null, 2)}</pre></> : <Notice title="Manifest unavailable">No manifest was supplied for this observation.</Notice>)}
    {active === 'logs' && <LogPanel key={r.id} applicationId={application.id} resource={r} />}
    {active === 'access' && <AccessPanel key={r.id} applicationId={application.id} resource={r} />}
  </div>;
}

/** Keep evidence from the last observation when a failed read returns no data.
 * A complete, connected empty snapshot remains authoritative (for example deletion).
 * Pagination stops until observation recovers, so retained and new pages cannot mix.
 */
function retainUnavailableInventory(previous: unknown, incoming: unknown) {
  const next = incoming as InfiniteData<ResourcePage, string | null>;
  const old = previous as InfiniteData<ResourcePage, string | null> | undefined;
  const failedEmpty = next.pages.length > 0 && next.pages.every(page => page.items.length === 0 && page.freshness.state === 'unavailable' && (page.partial?.length ?? 0) > 0);
  const retained = old?.pages.flatMap(page => page.items) ?? [];
  if (!failedEmpty || retained.length === 0 || !old) return next;
  return {
    ...next,
    pages: [{
      ...next.pages[0],
      items: retained,
      cursor: old.pages[0].cursor,
      next_page: null,
      freshness: { ...next.pages[0].freshness, observed_at: old.pages[0].freshness.observed_at },
      partial: next.pages.flatMap(page => page.partial ?? []),
    }],
    pageParams: [null],
  };
}
