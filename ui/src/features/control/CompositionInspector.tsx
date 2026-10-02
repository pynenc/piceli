import { Link } from 'react-router-dom';
import { Badge, formatTime, Notice } from '../../components/State';
import { githubCommitUrl, githubRepositoryUrl } from '../../components/sourceLinks';
import type { Component, Environment, Source, Verification } from './Composition';
import type { CompositionSelection } from './compositionGraph';
import { ApplicationDestinations, Counterparts, RefMatches, SourceDestinations, type InspectComponent } from './InspectorConnections';
import { componentPath, sourcePath } from './versionEvidence';
import './composition-evidence.css';

type Props = {
  selection: CompositionSelection | null; environment: Environment; component?: Component; source?: Source;
  requestedComponent: string | null; onClear: () => void; environments?: Environment[]; sources?: Source[];
  onInspect?: InspectComponent; onInspectSource?: (name: string) => void;
};

export function CompositionInspector({ selection, environment, component, source, requestedComponent, onClear, environments = [], sources = [], onInspect, onInspectSource }: Props) {
  const kind = selection?.kind ?? 'component';
  const missingComponent = requestedComponent !== null && !component;
  const resolvedSource = kind === 'component' ? sources.find(item => item.name === component?.source) ?? (source?.name === component?.source ? source : undefined) : source;
  const repositoryUrl = githubRepositoryUrl(resolvedSource?.url);
  const commitUrl = githubCommitUrl(resolvedSource?.url, component?.commit);
  const environmentPath = `/composition/environments/${encodeURIComponent(environment.name)}`;
  return <aside className="panel composition-inspector version-story" aria-label={`Selected ${kind}`}><div className="panelhead"><div><p className="eyebrow">Selected {kind}</p><h2>{kind === 'source' ? selection?.name : kind === 'environment' ? environment.name : component?.name ?? (missingComponent ? 'Component unavailable' : 'No component selected')}</h2><p className="small muted">{kind === 'source' ? source?.url ?? 'Source details not reported' : `${environment.name} / ${environment.namespace ?? 'Namespace pending'}`}</p></div></div>
    {kind === 'source' ? <div className="panelbody inspector-content">
      <section className="inspector-section"><div className="inspector-section-title"><span className="eyebrow">Published source</span><h3>Refs and exact commits</h3></div><dl className="facts"><dt>Last poll</dt><dd>{formatTime(source?.last_poll)}</dd><dt>Tracked refs</dt><dd>{source ? Object.keys(source.refs).length : 'Not reported'}</dd></dl>{source?.error && <Notice title="Source observation failed" danger>{source.error}</Notice>}
        {source && Object.keys(source.refs).length ? <ul className="composition-inspector-refs inspector-ref-list">{Object.entries(source.refs).map(([ref, sha]) => { const url = githubCommitUrl(source.url, sha); return <li key={ref}><strong>{ref}</strong><code>{sha}</code>{url && <a href={url} target="_blank" rel="noreferrer" aria-label={`View ${ref} commit in GitHub`}>View commit ↗</a>}</li>; })}</ul> : <p className="inspector-missing">No refs have been reported for this source.</p>}
        {repositoryUrl && <a className="button" href={repositoryUrl} target="_blank" rel="noreferrer">Open repository ↗</a>}
      </section>{source && <SourceDestinations source={source} environments={environments} onInspect={onInspect} />}<Link className="composition-inspector-link" to="/composition/sources">Inspect all sources →</Link>
    </div> : kind === 'environment' ? <div className="panelbody inspector-content">
      <section className="inspector-section"><dl className="facts"><dt>State</dt><dd><Badge value={environment.state} /></dd><dt>Health</dt><dd><Badge value={environment.health} /></dd><dt>Components</dt><dd>{environment.components.length}</dd><dt>Last sync</dt><dd>{formatTime(environment.last_sync)}</dd><dt>Last action</dt><dd>{environment.last_action ? <Badge value={environment.last_action} /> : 'Not reported'}</dd></dl>{environment.reason && <Notice title="Environment status">{environment.reason}</Notice>}<EnvironmentVerification verification={environment.verification} />
        <h3>Tracked revisions</h3><ul className="composition-inspector-refs">{Object.entries(environment.revision).map(([name, sha]) => <li key={name}>{onInspectSource ? <button className="inspector-text-button" onClick={() => onInspectSource(name)}>{name} →</button> : <Link to={sourcePath(environment.name, name)}>{name} →</Link>}<code>{sha}</code></li>)}</ul>{!Object.keys(environment.revision).length && <p className="inspector-missing">No source revisions reported.</p>}
        <Link className="button" to={environmentPath}>Open {environment.name}</Link>
      </section><section className="inspector-section"><h3>Explore components</h3><ul className="inspector-component-links">{environment.components.map(item => <li key={item.name}>{onInspect ? <button onClick={() => onInspect(environment.name, item.name)}>{item.name} →</button> : <Link to={componentPath(environment.name, item.name)}>{item.name} →</Link>}<Badge value={item.state} /></li>)}</ul>{!environment.components.length && <p className="inspector-missing">No components reported.</p>}</section><ApplicationDestinations environment={environment} />
    </div> : component ? <div className="panelbody inspector-content">
      <section className="inspector-section inspector-component-state"><dl className="facts"><dt>State</dt><dd><Badge value={component.state} /></dd><dt>Health</dt><dd><Badge value={component.health} /></dd><dt>Updated</dt><dd>{formatTime(component.updated_at)}</dd></dl>{component.reason && <Notice title="Component status">{component.reason}</Notice>}{resolvedSource?.error && <Notice title="Source observation failed" danger>{resolvedSource.error}</Notice>}</section>
      <section className="inspector-section" aria-label="Selected version evidence"><div className="inspector-section-title"><span className="eyebrow">Version identity</span><h3>Source → image → environment</h3></div><ol className="inspector-version-chain">
        <li><span aria-hidden="true">01</span><div><dl><dt>Source</dt><dd>{component.source ? onInspectSource ? <button className="inspector-text-button" aria-label={`Inspect source ${component.source}`} onClick={() => onInspectSource(component.source!)}>{component.source} →</button> : <Link to={sourcePath(environment.name, component.source)}>{component.source} →</Link> : 'Not reported'}</dd><dt>Commit</dt><dd><code>{component.commit ?? 'Not resolved yet'}</code></dd></dl><RefMatches source={resolvedSource} commit={component.commit} />{commitUrl && <a className="inspector-text-link" href={commitUrl} target="_blank" rel="noreferrer">View commit in GitHub ↗</a>}</div></li>
        <li><span aria-hidden="true">02</span><div><dl><dt>Image digest</dt><dd><code>{component.digest ?? 'Image unreported'}</code></dd></dl><p className="inspector-context">{component.digest ? 'Image identity reported for this component.' : 'No image identity is reported; build or workload status cannot be inferred.'}</p></div></li>
        <li><span aria-hidden="true">03</span><div><strong>{environment.name}</strong><p className="inspector-context">{environment.namespace ?? 'Namespace pending'}</p><Link className="inspector-text-link" to={environmentPath}>Open {environment.name}</Link></div></li>
      </ol></section>
      <Counterparts component={component} environment={environment} environments={environments} onInspect={onInspect} />
      <ApplicationDestinations environment={environment} />
    </div> : missingComponent ? <div className="panelbody"><Notice title="Selected component unavailable"><p><code>{requestedComponent}</code> is not in the reported environment. Select another component or clear this selection.</p><button onClick={onClear}>Clear component selection</button></Notice></div> : <p className="panelbody small muted">Select an environment with reported components to inspect its source, state and image.</p>}
  </aside>;
}

/** The controller's checks verification; failing checks never roll the release back. */
export function EnvironmentVerification({ verification }: { verification?: Verification | null }) {
  if (!verification) return null;
  const failed = verification.state === 'failed';
  return <Notice title={failed ? 'Checks verification failed' : 'Checks verified'} danger={failed}>
    <p className="small">{verification.trigger === 'checks-changed' ? 'The check set changed, so the checks ran against the running release.' : 'The checks ran against the running release.'} Nothing was applied{failed ? ' and nothing was rolled back' : ''}.{verification.at ? ` ${formatTime(verification.at)}.` : ''}</p>
    {failed && (verification.failed.length ? <ul className="small">{verification.failed.map((item, index) => <li key={index}><strong>{item.check ?? 'check'}</strong> · <code>{item.code ?? 'check-failed'}</code></li>)}</ul> : <p className="small">No failing check was reported.</p>)}
    {verification.checks_hash && <p className="small muted">Checks <code>{verification.checks_hash.slice(0, 12)}</code></p>}
  </Notice>;
}
