import { Link, useSearchParams } from 'react-router-dom';
import { Badge, formatTime } from '../../components/State';
import { githubCommitUrl, githubRepositoryUrl } from '../../components/sourceLinks';
import type { Environment, Source } from './Composition';
import './source-inventory.css';

function destinations(source: Source, environments: Environment[]) {
  return environments.filter(environment => Object.hasOwn(environment.revision, source.name) || environment.components.some(component => component.source === source.name));
}

function SourceRef({ source, name, revision }: { source: Source; name: string; revision: string }) {
  const commit = githubCommitUrl(source.url, revision);
  return <details className="source-ref" aria-label={`Revision for ${source.name} ref ${name}`}><summary><span className="source-ref-name" title={name}>{name}</span><code title={revision}>{revision ? revision.slice(0, 7) : 'unknown'}</code></summary><div className="source-ref-evidence"><code>{revision || 'Revision not reported'}</code>{commit && <a href={commit} target="_blank" rel="noreferrer">Open commit<span aria-hidden="true"> ↗</span></a>}</div></details>;
}

function SourceDestinations({ source, environments }: { source: Source; environments: Environment[] }) {
  const related = destinations(source, environments);
  return <section className="source-destinations" aria-label={`Destinations for ${source.name}`}><h3>Used by <span>{related.length}</span></h3>{related.length ? <ul>{related.map(environment => {
    const components = environment.components.filter(component => component.source === source.name);
    const topology = `/composition/overview?${new URLSearchParams({ environment: environment.name, node: 'source', source: source.name })}`;
    return <li key={environment.name}><div className="source-destination-identity"><Link to={`/composition/environments/${encodeURIComponent(environment.name)}`} aria-label={`Environment ${environment.name}`}>{environment.name}</Link><span>{environment.namespace ?? 'Namespace pending'}</span></div><div className="source-destination-components">{components.length ? components.map(component => <Link key={component.name} to={`/composition/overview?${new URLSearchParams({ environment: environment.name, component: component.name })}`} aria-label={`Inspect ${component.name} in ${environment.name}`}>{component.name}</Link>) : <span className="source-missing-association">No component source reported</span>}</div><Link className="source-topology-link" to={topology} aria-label={`View ${source.name} topology in ${environment.name}`} title={`View ${source.name} topology in ${environment.name}`}><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true"><rect x="1" y="6" width="5" height="7" rx="1" /><rect x="13" y="2" width="5" height="5" rx="1" /><rect x="13" y="12" width="5" height="5" rx="1" /><path d="M6 9.5h4V4.5h3M10 9.5v5h3" /></svg></Link></li>;
  })}</ul> : <p className="source-empty-association">No environment references this source in the reported snapshot.</p>}</section>;
}

/** One row per reported source; destinations are joined by source identity only. */
export function SourceInventory({ sources, environments }: { sources: Source[]; environments: Environment[] }) {
  const [params, setParams] = useSearchParams();
  const search = params.get('sourceSearch') ?? '';
  const needle = search.trim().toLowerCase();
  const shown = sources.filter(source => {
    const related = destinations(source, environments);
    const values = [source.name, source.url, source.error, ...Object.entries(source.refs).flat(), ...related.flatMap(environment => [environment.name, environment.namespace, ...environment.components.filter(component => component.source === source.name).map(component => component.name)])];
    return !needle || values.some(value => value?.toLowerCase().includes(needle));
  });
  const updateSearch = (value: string) => setParams(previous => { const next = new URLSearchParams(previous); if (value) next.set('sourceSearch', value); else next.delete('sourceSearch'); return next; }, { replace: true });
  if (!sources.length) return <section className="panel empty"><h2>No sources reported</h2><p>The controller has not published any source repositories.</p></section>;
  return <div className="source-inventory">
    <div className="source-inventory-tools"><label><span>Filter sources</span><input type="search" placeholder="Repository, ref, commit or destination…" value={search} onChange={event => updateSearch(event.target.value)} /></label><p aria-live="polite">{shown.length} of {sources.length} sources</p>{search && <button onClick={() => updateSearch('')}>Clear source filter</button>}</div>
    <div className="source-inventory-columns" aria-hidden="true"><span>Repository & observation</span><span>Tracked refs</span><span>Environments & components</span></div>
    {shown.length ? shown.map(source => {
      const repository = githubRepositoryUrl(source.url);
      return <section key={source.name} className="source-inventory-row" aria-label={`Source ${source.name}`}>
        <div className="source-identity"><div className="source-identity-heading"><h2>{source.name}</h2><Badge value={source.error ? 'failed' : source.last_poll ? 'observed' : 'unknown'} /></div><code className="source-url">{source.url ?? 'URL not reported'}</code><div className="source-observation"><span>Last poll <time dateTime={source.last_poll ?? undefined}>{formatTime(source.last_poll)}</time></span>{repository && <a href={repository} target="_blank" rel="noreferrer">Open repository<span aria-hidden="true"> ↗</span></a>}</div>{source.error && <p className="source-error">{source.error}</p>}</div>
        <section className="source-refs-column" aria-label={`Refs for ${source.name}`}><h3>Refs <span>{Object.keys(source.refs).length}</span></h3>{Object.keys(source.refs).length ? <div className="source-refs">{Object.entries(source.refs).map(([name, revision]) => <SourceRef key={name} source={source} name={name} revision={revision} />)}</div> : <p className="source-empty-association">No ref resolved yet.</p>}</section>
        <SourceDestinations source={source} environments={environments} />
      </section>;
    }) : <section className="source-no-matches"><h2>No matching sources</h2><p>Search by repository, ref, full revision, environment or component.</p></section>}
  </div>;
}
