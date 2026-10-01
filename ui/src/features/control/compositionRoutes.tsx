// Every registration of the composition views (nav, title, routes) lives here,
// so App.tsx only calls these three functions.
import { lazy } from 'react';
import { NavLink, Route } from 'react-router-dom';
import type { Capabilities } from '../../api/generated';
import { Notice } from '../../components/State';
import { NamedEnvironmentActions } from './NamedEnvironmentActions';

const CompositionEnvironments = lazy(async () => ({ default: (await import('./Composition')).CompositionEnvironments }));
const CompositionEnvironment = lazy(async () => ({ default: (await import('./Composition')).CompositionEnvironment }));
const CompositionSources = lazy(async () => ({ default: (await import('./Composition')).CompositionSources }));

const allowed = (capabilities?: Capabilities) => capabilities?.actions.composition?.allowed === true;

function Unavailable() {
  return <Notice title="Composition views unavailable">They are served by the UI that <code>piceli cluster init</code> installs; open it with <code>piceli access ui</code>.</Notice>;
}

export function compositionNav(capabilities?: Capabilities) {
  if (!allowed(capabilities)) return null;
  return <><NavLink to="/composition" end>◈ <span>Environments</span></NavLink><NavLink to="/composition/sources">⑂ <span>Sources</span></NavLink></>;
}

export function compositionTitle(pathname: string): string | null {
  if (pathname.startsWith('/composition/sources')) return 'Sources';
  return pathname.startsWith('/composition') ? 'Environments' : null;
}

export function compositionHome(capabilities?: Capabilities): string | null {
  return allowed(capabilities) ? '/composition' : null;
}

export function compositionRoutes(capabilities?: Capabilities) {
  const canSync = capabilities?.actions.composition_sync?.allowed === true;
  const ok = allowed(capabilities);
  return <>
    <Route path="/composition" element={ok ? <CompositionEnvironments canSync={canSync} /> : <Unavailable />} />
    <Route path="/composition/sources" element={ok ? <CompositionSources /> : <Unavailable />} />
    <Route path="/composition/environments/:env" element={ok ? <CompositionEnvironment canSync={canSync} actions={environment => <NamedEnvironmentActions environment={environment} canChange={canSync} />} /> : <Unavailable />} />
  </>;
}
