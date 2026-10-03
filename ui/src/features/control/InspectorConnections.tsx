import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, applicationPath, logsPath, forwardsPath } from '../../api/client';
import { Badge, Failure } from '../../components/State';
import type { Component, Environment, Source } from './Composition';
import { compareComponentVersions, comparisonPath, componentPath, fullCommit, matchingRefs } from './versionEvidence';

export type InspectComponent = (environment: string, component: string) => void;

export function Counterparts({ component, environment, environments, onInspect }: { component: Component; environment: Environment; environments: Environment[]; onInspect?: InspectComponent }) {
  const matches = component.source ? environments.flatMap(item => item.name === environment.name ? [] : item.components.filter(other => other.name === component.name && other.source === component.source).map(other => ({ environment: item, component: other }))) : [];
  return <section className="inspector-section" aria-label="Cross-environment versions"><div className="inspector-section-title"><span className="eyebrow">Across environments</span><h3>Where else is {component.name}?</h3></div>
    {!component.source ? <p className="inspector-missing">A source identity is needed to match this component across environments.</p> : matches.length === 0 ? <p className="inspector-missing">No other environment reports this component from {component.source}.</p> : <><p className="inspector-context">Same component and source. Versions are compared by complete identities; state is shown separately.</p><div className="inspector-counterparts">{matches.map(item => {
      const compared = compareComponentVersions(component, item.component);
      return <article key={item.environment.name} aria-label={`${component.name} in ${item.environment.name}`}><header>{onInspect ? <button aria-label={`Open ${component.name} counterpart in ${item.environment.name}`} onClick={() => onInspect(item.environment.name, component.name)}>{item.environment.name}<span aria-hidden="true">↗</span></button> : <Link to={componentPath(item.environment.name, component.name)}>{item.environment.name} ↗</Link>}<Badge value={item.component.state} /></header><div className="inspector-version-result"><span data-comparison={compared.commit}>{compared.commit === 'same' ? 'Same commit' : compared.commit === 'different' ? 'Different commit' : !item.component.commit ? 'Commit unreported' : !fullCommit(item.component.commit) || !fullCommit(component.commit) ? 'Full commit needed' : 'Commit unknown'}</span><span data-comparison={compared.digest}>{compared.digest === 'same' ? 'Same image' : compared.digest === 'different' ? 'Different image' : !item.component.digest ? 'Image unreported' : 'Selected image unreported'}</span></div><details><summary>Exact version in {item.environment.name}</summary><dl><dt>Commit</dt><dd><code>{item.component.commit ?? 'Not reported'}</code></dd><dt>Image digest</dt><dd><code>{item.component.digest ?? 'Not reported'}</code></dd></dl></details><Link className="inspector-text-link" to={comparisonPath(environment.name, item.environment.name, component.name)}>Compare versions</Link></article>;
    })}</div></>}
  </section>;
}

export function SourceDestinations({ source, environments, onInspect }: { source: Source; environments: Environment[]; onInspect?: InspectComponent }) {
  const destinations = environments.filter(environment => Object.hasOwn(environment.revision, source.name) || environment.components.some(component => component.source === source.name));
  return <section className="inspector-section" aria-label={`Reported destinations for ${source.name}`}><div className="inspector-section-title"><span className="eyebrow">Source destinations</span><h3>Where this source is reported</h3></div>{destinations.length ? <div className="inspector-source-destinations">{destinations.map(environment => <article key={environment.name}><header><Link to={`/composition/environments/${encodeURIComponent(environment.name)}`}>{environment.name}</Link><Badge value={environment.state} /></header>{environment.components.filter(component => component.source === source.name).map(component => <div key={component.name}>{onInspect ? <button onClick={() => onInspect(environment.name, component.name)} aria-label={`Open ${component.name} from ${source.name} in ${environment.name}`}>{component.name} →</button> : <Link to={componentPath(environment.name, component.name)}>{component.name} →</Link>}<span>{component.commit ? <code title={component.commit}>{component.commit.slice(0, 12)}</code> : 'Commit unreported'}</span></div>)}{!environment.components.some(component => component.source === source.name) && <p className="inspector-missing">Environment revision references this source; no component relationship is reported.</p>}</article>)}</div> : <p className="inspector-missing">No reported environment references this source.</p>}</section>;
}

export function RefMatches({ source, commit }: { source?: Source; commit?: string | null }) {
  const refs = matchingRefs(source, commit);
  if (!source) return <p className="inspector-missing">Source details are not reported. Published refs cannot be matched.</p>;
  if (!fullCommit(commit)) return <p className="inspector-missing">A full commit identity is needed to match published refs.</p>;
  return refs.length ? <div className="inspector-ref-matches"><span>Published refs at this commit</span><ul aria-label="Published refs at this commit">{refs.map(ref => <li key={ref}>{ref}</li>)}</ul></div> : <p className="inspector-missing">This exact commit does not match a currently reported ref. Its ancestry is not known.</p>;
}

export function ApplicationDestinations({ environment, component }: { environment: Environment; component?: string }) {
  const id = environment.application_id;
  const query = useQuery({ queryKey: ['application', id], queryFn: ({ signal }) => api.application(id!, signal), enabled: Boolean(id) });
  const application = query.data?.id === id ? query.data : undefined;
  return <section className="inspector-section inspector-application" aria-label="Observed application links"><div className="inspector-section-title"><span className="eyebrow">Live application</span><h3>Explore {environment.name}</h3></div>
    {!id ? <p className="inspector-missing">No application scope is reported for this environment.</p> : <>{query.isPending && <p className="inspector-missing" role="status">Checking available application views…</p>}{query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}{application && <><p className="inspector-context">{application.target.name} / {application.target.namespace}</p><div className="inspector-application-links">{application.capabilities?.inspect?.allowed && <Link to={`${applicationPath(id)}/resources`}>Inspect workloads and logs →</Link>}<Link to={logsPath({ scope: id, workload: component })}>{component ? `Logs of ${component}` : `Logs of ${environment.name}`} →</Link><Link to={forwardsPath({ application: id })}>Forward a port →</Link>{application.capabilities?.activity?.allowed && <Link to={`${applicationPath(id)}/activity`}>Open deployment activity →</Link>}{application.capabilities?.evaluate?.allowed && <Link to={`${applicationPath(id)}/changes`}>Review application changes →</Link>}</div>{!application.capabilities?.inspect?.allowed && !application.capabilities?.activity?.allowed && !application.capabilities?.evaluate?.allowed && <p className="inspector-missing">No application views are available in this session.</p>}</>}</>}
  </section>;
}
