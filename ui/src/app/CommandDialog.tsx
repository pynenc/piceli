import { useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, applicationPath } from '../api/client';
import type { Capabilities } from '../api/generated';
import { Icon } from '../components/Icon';
import { Failure } from '../components/State';

type Destination = { label: string; detail: string; path: string; kind: string };
type CompositionIndex = { environments: { name: string; namespace?: string | null; components: { name: string; source?: string | null }[] }[] };

export function CommandDialog({ capabilities, onClose, restoreFocus }: { capabilities?: Capabilities; onClose: () => void; restoreFocus: () => void }) {
  const open = true;
  const [search, setSearch] = useState('');
  const navigating = useRef(false);
  const applications = useInfiniteQuery({ queryKey: ['applications'], initialPageParam: null as string | null, queryFn: ({ signal, pageParam }) => api.applications(pageParam, signal), getNextPageParam: page => page.next_page ?? undefined, enabled: open && Boolean(capabilities) });
  const composition = useQuery({ queryKey: ['composition'], queryFn: ({ signal }) => api.composition(signal).then(value => value as CompositionIndex), enabled: open && capabilities?.actions.composition?.allowed === true });
  const destinations: Destination[] = [{ label: 'Applications', detail: 'Registered definitions and targets', path: '/applications', kind: 'Workspace' }];
  if (capabilities?.actions.composition?.allowed) destinations.unshift(
    { label: 'Infrastructure', detail: 'Sources, components and environments', path: '/composition/overview', kind: 'Workspace' },
    { label: 'Versions', detail: 'Compare reported environment revisions', path: '/composition/overview?view=versions', kind: 'Workspace' },
    { label: 'Attention', detail: 'Reported problems and pending decisions', path: '/composition/overview?view=attention', kind: 'Workspace' },
    { label: 'Environments', detail: 'Composition inventory', path: '/composition', kind: 'Workspace' },
    { label: 'Sources', detail: 'Repositories and published revisions', path: '/composition/sources', kind: 'Workspace' },
  );
  for (const [capability, label, path] of [['activity', 'Deployment history', '/delivery'], ['pipeline', 'Pipeline', '/pipeline'], ['cluster_build', 'Cluster build', '/cluster-build'], ['gitops', 'GitOps', '/gitops'], ['cluster_status', 'Cluster', '/cluster'], ['environments', 'Branch environments', '/environments']]) {
    if (capabilities?.actions[capability]?.allowed) destinations.push({ label, detail: 'Open workspace page', path, kind: 'Workspace' });
  }
  const apps = applications.data?.pages.flatMap(page => page.items) ?? [];
  for (const app of apps) destinations.push({ label: app.name, detail: `${app.target.name} / ${app.target.namespace}`, path: `${applicationPath(app.id)}/overview`, kind: 'Application' });
  if (capabilities?.actions.composition?.allowed) for (const environment of composition.data?.environments ?? []) {
    destinations.push({ label: environment.name, detail: environment.namespace ?? 'Namespace pending', path: `/composition/environments/${encodeURIComponent(environment.name)}`, kind: 'Environment' });
    for (const component of environment.components) destinations.push({ label: component.name, detail: `${environment.name} · ${component.source ?? 'Source not reported'}`, path: `/composition/overview?${new URLSearchParams({ environment: environment.name, component: component.name })}`, kind: 'Component' });
  }
  const needle = search.trim().toLocaleLowerCase();
  const results = destinations.filter(item => `${item.label} ${item.detail} ${item.kind}`.toLocaleLowerCase().includes(needle));
  return <Dialog.Root open={open} onOpenChange={value => { if (!value) onClose(); }}>
    <Dialog.Portal><Dialog.Overlay className="dialog-overlay" /><Dialog.Content className="command-dialog" onOpenAutoFocus={event => { event.preventDefault(); document.getElementById('workspace-search')?.focus(); }} onCloseAutoFocus={event => { event.preventDefault(); if (navigating.current) document.getElementById('main')?.focus(); else restoreFocus(); }}>
      <div className="command-heading"><div><Dialog.Title>Go anywhere</Dialog.Title><Dialog.Description>Find an application, environment, component or page.</Dialog.Description></div><Dialog.Close aria-label="Close workspace search">Esc</Dialog.Close></div>
      <label className="command-search"><Icon name="search" /><span className="sr-only">Search destinations</span><input id="workspace-search" type="search" placeholder="Try a component, environment or namespace…" value={search} onChange={event => setSearch(event.target.value)} autoComplete="off" /></label>
      <div className="command-results" aria-label="Workspace destinations">
        {(applications.isFetching || composition.isFetching) && <p className="small muted" role="status">Updating destinations…</p>}
        {applications.isError && <Failure error={applications.error} retry={() => void applications.refetch()} />}
        {capabilities?.actions.composition?.allowed && composition.isError && <Failure error={composition.error} retry={() => void composition.refetch()} />}
        <ul>{results.slice(0, 80).map(item => <li key={`${item.kind}:${item.path}`}><Link to={item.path} aria-label={`${item.kind}: ${item.label}, ${item.detail}`} onClick={() => { navigating.current = true; onClose(); }}><span className="command-kind">{item.kind}</span><strong>{item.label}</strong><span className="command-detail">{item.detail}</span><Icon name="arrow" /></Link></li>)}</ul>
        {results.length === 0 && <p className="empty">No matching destinations in this session.</p>}
        {results.length > 80 && <p className="small muted">Showing 80 matches. Refine your search to find a specific destination.</p>}
        {applications.hasNextPage && <button disabled={applications.isFetchingNextPage} onClick={() => void applications.fetchNextPage()}>Load more applications</button>}
      </div><footer className="command-footer"><span>Tab to a result · Enter to open</span><span>Ctrl / ⌘ K to search</span></footer>
    </Dialog.Content></Dialog.Portal>
  </Dialog.Root>;
}
