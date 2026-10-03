import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import { Icon } from '../components/Icon';
import type { Environment } from '../features/control/Composition';

type CompositionContext = { configured: boolean; environments: Environment[] };

/** Only a controller-published application ID establishes this relationship. */
export function ApplicationEnvironmentContext({ applicationId, allowed }: { applicationId: string; allowed: boolean }) {
  const query = useQuery({ queryKey: ['composition'], queryFn: ({ signal }) => api.composition(signal).then(value => value as CompositionContext), enabled: allowed });
  if (!allowed || !query.data?.configured) return null;
  const environments = query.data.environments.filter(environment => environment.application_id === applicationId);
  if (!environments.length) return null;
  return <nav className="application-environment-context" aria-label="Related infrastructure"><span className="environment-context-label"><Icon name="overview" /> Composition</span>{environments.map(environment => <div key={environment.name}>
    <Link className="environment-context-name" to={`/composition/overview?${new URLSearchParams({ environment: environment.name, node: 'environment' })}`}>{environment.name} <span aria-hidden="true">↗</span></Link>
    <span className="environment-context-components">{environment.components.map(component => <Link key={component.name} to={`/composition/overview?${new URLSearchParams({ environment: environment.name, component: component.name })}`} aria-label={`Inspect ${component.name} in ${environment.name}`}>{component.name}</Link>)}</span>
    <Link className="environment-context-versions" to={`/composition/overview?${new URLSearchParams({ view: 'versions', baseline: environment.name })}`}>Compare versions →</Link>
  </div>)}{query.isError && <span className="stale" role="status">Last observed composition</span>}</nav>;
}
