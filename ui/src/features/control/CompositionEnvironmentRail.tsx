import { Badge } from '../../components/State';
import type { Environment } from './Composition';

/** A switchboard over reported environments, not inferred deployment targets. */
export function CompositionEnvironmentRail({ environments, selected, allEnvironments, onSelect }: { environments: Environment[]; selected?: string; allEnvironments: boolean; onSelect: (name: string) => void }) {
  return <div className="composition-environment-rail" role="group" aria-label="Environment switchboard">
    {environments.map(environment => {
      const revisions = Object.entries(environment.revision);
      return <button key={environment.name} className="composition-environment-stop" aria-label={`Show environment ${environment.name}`} aria-pressed={!allEnvironments && selected === environment.name} onClick={() => onSelect(environment.name)}>
        <span className="composition-environment-stop-heading"><strong>{environment.name}</strong><Badge value={environment.state} /></span>
        <span className="composition-environment-stop-meta"><span>{environment.components.length} {environment.components.length === 1 ? 'component' : 'components'}</span><span>{environment.health}</span></span>
        <span className="composition-environment-stop-versions">{revisions.length ? <>{revisions.slice(0, 2).map(([source, sha]) => <span key={source}><span>{source}</span><code title={sha}>{sha.slice(0, 7)}</code></span>)}{revisions.length > 2 && <span className="muted">+{revisions.length - 2} sources</span>}</> : <span className="muted">Revision unreported</span>}</span>
      </button>;
    })}
  </div>;
}
