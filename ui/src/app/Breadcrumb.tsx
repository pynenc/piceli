import { Link, useLocation, useMatch } from 'react-router-dom';
import { compositionTitle } from '../features/control/compositionRoutes';

const applicationViews: Record<string, string> = { overview: 'Overview', resources: 'Resources', changes: 'Changes', plans: 'Plans', activity: 'Activity' };
const pages: Record<string, string> = { '/delivery': 'Deployment history', '/applications': 'Applications', '/cluster': 'Cluster', '/pipeline': 'Pipeline', '/cluster-build': 'Cluster build', '/environments': 'Environments', '/gitops': 'GitOps', '/logs': 'Logs', '/forwards': 'Port forwards', '/dev-builds': 'Development builds' };

/** The route names the view; the existing page heading supplies its live identity. */
export function Breadcrumb() {
  const { pathname } = useLocation();
  const application = useMatch('/applications/:applicationId/:tab');
  const environment = useMatch('/composition/environments/:env');
  const run = useMatch('/runs/:operationId');
  const parent = application ? { label: 'Applications', path: '/applications' } : environment ? { label: 'Environments', path: '/composition' } : null;
  const current = application
    ? Object.hasOwn(applicationViews, application.params.tab ?? '') ? applicationViews[application.params.tab!] : 'View unavailable'
    : environment ? environment.params.env
      : run ? 'Run details'
        : compositionTitle(pathname) ?? (Object.hasOwn(pages, pathname) ? pages[pathname] : pathname === '/' ? 'Connecting' : 'Page not found');
  return <nav className="breadcrumb" aria-label="Breadcrumb"><ol>
    <li className="breadcrumb-workspace">Workspace</li>
    {parent && <li><span aria-hidden="true">/</span><Link to={parent.path}>{parent.label}</Link></li>}
    <li><span aria-hidden="true">/</span><strong aria-current="page">{current}</strong></li>
  </ol></nav>;
}
