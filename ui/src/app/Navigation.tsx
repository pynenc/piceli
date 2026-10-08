import { useEffect, useId, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, NavLink, useLocation, type Location } from 'react-router-dom';
import { api } from '../api/client';
import type { Capabilities, NavigationSummary } from '../api/generated';
import { Icon } from '../components/Icon';
import './navigation.css';

const STORAGE = 'piceli.nav.open';
const environmentPath = (name: string) => `/composition/environments/${encodeURIComponent(name)}`;

function remembered(): Record<string, boolean> {
  try {
    const value = JSON.parse(window.localStorage.getItem(STORAGE) ?? '{}');
    return value && typeof value === 'object' && !Array.isArray(value) ? Object.fromEntries(Object.entries(value).filter(([, open]) => typeof open === 'boolean')) as Record<string, boolean> : {};
  } catch { return {}; }
}

type Tone = 'danger' | 'warning';
function Count({ value, tone, label }: { value?: number | null; tone: Tone; label: string }) {
  if (!value) return null;
  // Beside the link, not in its name: the page keeps its plain name.
  return <span className={`nav-count ${tone}`} title={`${value} ${label}`}>{value}<span className="sr-only"> {label}</span></span>;
}

type Item = { to: string; label: string; icon: Parameters<typeof Icon>[0]['name']; end?: boolean; badge?: ReactNode; children?: { to: string; label: string; state?: string }[] };

/** A sub-menu link with a query or fragment is current only when those match too. */
function childCurrent(to: string, location: Location) {
  const url = new URL(to, 'http://piceli.invalid');
  if (url.pathname !== location.pathname) return false;
  if (url.hash && url.hash !== location.hash) return false;
  const current = new URLSearchParams(location.search);
  return [...url.searchParams].every(([key, value]) => current.get(key) === value) && (url.search !== '' || url.hash !== '' || !['view'].some(key => current.has(key)));
}

function Entry({ item, open, toggle }: { item: Item; open: boolean; toggle: () => void }) {
  const id = useId();
  const location = useLocation();
  const children = item.children ?? [];
  return <div className={`nav-entry${children.length ? ' has-children' : ''}`}>
    <div className="nav-row"><NavLink to={item.to} end={item.end}><Icon name={item.icon} /><span>{item.label}</span></NavLink>{item.badge}
      {children.length > 0 && <button className="nav-toggle" aria-expanded={open} aria-controls={id} onClick={toggle}><span className="sr-only">{open ? 'Collapse' : 'Expand'} {item.label}</span><span aria-hidden="true">›</span></button>}</div>
    {children.length > 0 && <ul id={id} className="nav-children" hidden={!open}>{children.map(child => { const active = childCurrent(child.to, location); return <li key={child.to}><Link to={child.to} className={active ? 'active' : undefined} aria-current={active ? 'page' : undefined}>{child.state && <i className={`nav-dot ${child.state}`} aria-hidden="true" />}<span>{child.label}</span></Link></li>; })}</ul>}
  </div>;
}

/** Grouped pages with alert badges and collapsible sub-menus; open state is remembered. */
export function Navigation({ capabilities }: { capabilities?: Capabilities }) {
  const actions = capabilities?.actions ?? {};
  const allowed = (name: string) => actions[name]?.allowed === true;
  const summaryQuery = useQuery({ queryKey: ['navigation'], queryFn: ({ signal }) => api.navigation(signal), enabled: Boolean(capabilities), refetchInterval: 20000, retry: false });
  const summary: NavigationSummary = summaryQuery.data ?? {};
  const location = useLocation();
  // An explicit choice is remembered; otherwise the current page's sub-menu is open.
  const [choices, setChoices] = useState<Record<string, boolean>>(remembered);
  useEffect(() => { try { window.localStorage.setItem(STORAGE, JSON.stringify(choices)); } catch { /* private browsing */ } }, [choices]);
  const composition = allowed('composition');
  const environments = summary.environments ?? [];
  const tone = (health: string, state: string) => ['degraded', 'failed', 'unhealthy'].includes(health) || ['failed', 'degraded'].includes(state) ? 'bad' : state === 'approval-required' ? 'warn' : health === 'healthy' ? 'good' : 'neutral';
  const workspace: Item[] = [
    ...(composition ? [
      { to: '/composition/overview', label: 'Overview', icon: 'overview' as const, badge: !allowed('cluster_build') ? <Count value={summary.failed_builds} tone="danger" label="failed builds" /> : null },
      { to: '/composition', label: 'Environments', icon: 'environments' as const, end: true, badge: <Count value={summary.degraded_environments} tone="danger" label="degraded environments" />, children: environments.map(env => ({ to: environmentPath(env.name), label: env.name, state: tone(env.health, env.state) })) },
      { to: '/composition/sources', label: 'Sources', icon: 'sources' as const },
    ] : []),
    { to: '/applications', label: 'Applications', icon: 'applications' },
    ...(allowed('cluster_status') ? [{ to: '/cluster', label: 'Cluster', icon: 'cluster' as const, badge: <Count value={summary.registry_warnings} tone="warning" label="registry warnings" />, children: [{ to: '/cluster#nodes', label: 'Nodes' }, { to: '/cluster#registry', label: 'Registry' }] }] : []),
    ...(allowed('machines') ? [{ to: '/machines', label: 'Machines', icon: 'cluster' as const }] : []),
  ];
  const history = allowed('activity') || allowed('composition_history');
  const delivery: Item[] = [
    ...(history ? [{ to: '/delivery', label: 'Deployment history', icon: 'history' as const, badge: <Count value={summary.approvals} tone="warning" label="approvals waiting" />, children: composition ? [{ to: '/delivery', label: 'History' }, { to: '/composition/overview?view=attention', label: 'Approvals' }] : [] }] : []),
    ...(allowed('pipeline') ? [{ to: '/pipeline', label: 'Pipeline', icon: 'pipeline' as const }] : []),
    ...(allowed('cluster_build') ? [{ to: '/cluster-build', label: 'Cluster build', icon: 'build' as const, badge: <Count value={summary.failed_builds} tone="danger" label="failed builds" /> }] : []),
  ];
  const operations: Item[] = [
    ...(allowed('logs') ? [{ to: '/logs', label: 'Logs', icon: 'logs' as const }] : []),
    ...(allowed('access') || composition ? [{ to: '/forwards', label: 'Port forwards', icon: 'forwards' as const, badge: <Count value={summary.stale_forwards} tone="warning" label="stale forwards" /> }] : []),
    ...(allowed('environments') ? [{ to: '/environments', label: composition ? 'Branch environments' : 'Environments', icon: 'environments' as const }] : []),
    ...(allowed('gitops') ? [{ to: '/gitops', label: 'GitOps', icon: 'gitops' as const }] : []),
    ...(allowed('dev_builds') ? [{ to: '/dev-builds', label: 'Development builds', icon: 'build' as const }] : []),
  ];
  const groups: [string, Item[]][] = [['Workspace', workspace], ['Delivery', delivery], ['Operations', operations]];
  const current = (item: Item) => location.pathname === item.to.split('?')[0] || (item.children ?? []).some(child => childCurrent(child.to, location));
  return <nav aria-label="Main navigation">{groups.filter(([, items]) => items.length).map(([label, items]) => <div className="nav-group" key={label}><p className="eyebrow nav-label">{label}</p>
    {items.map(item => { const shown = item.to in choices ? choices[item.to] : current(item); return <Entry key={item.to} item={item} open={shown} toggle={() => setChoices(value => ({ ...value, [item.to]: !shown }))} />; })}
  </div>)}</nav>;
}
