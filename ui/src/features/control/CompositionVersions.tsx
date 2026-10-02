import { Link, useSearchParams } from 'react-router-dom';
import { githubCommitUrl, githubCompareUrl } from '../../components/sourceLinks';
import { Notice } from '../../components/State';
import type { Component, Environment, Source } from './Composition';
import { compareComponentVersions, fullCommit, matchingRefs, sourcePath } from './versionEvidence';
import './composition-evidence.css';

export { compareComponentVersions } from './versionEvidence';

function VersionCell({ component, environment, source }: { component?: Component; environment: string; source?: Source }) {
  if (!component) return <span className="muted">Not reported in this environment</span>;
  const commitUrl = githubCommitUrl(source?.url, component.commit);
  const refs = matchingRefs(source, component.commit);
  return <div className="composition-version-cell">
    {component.source ? <Link className="version-source-link" to={sourcePath(environment, component.source)} aria-label={`Inspect source ${component.source}`}>{component.source} ↗</Link> : <span>Source unreported</span>}
    {commitUrl ? <a className="version-commit-link" href={commitUrl} target="_blank" rel="noreferrer" aria-label={`View ${component.name} commit in GitHub`}><code title={component.commit!}>{component.commit!.slice(0, 12)}</code> ↗</a> : <code title={component.commit ?? undefined}>{component.commit ? component.commit.slice(0, 12) : 'Commit unreported'}</code>}
    {refs.length ? <ul className="version-ref-chips" aria-label={`Published refs for ${component.name} in ${environment}`}>{refs.map(ref => <li key={ref}>{ref}</li>)}</ul> : <span className="version-evidence-note">{!source ? 'Source details unavailable' : !fullCommit(component.commit) ? 'Full commit identity unavailable' : 'No reported ref matches this commit'}</span>}
    <code title={component.digest ?? undefined}>{component.digest ? component.digest.length > 23 ? `${component.digest.slice(0, 23)}…` : component.digest : 'Image unreported'}</code>
    {(component.commit || component.digest) && <details><summary>Exact identities</summary><dl><dt>Commit</dt><dd><code>{component.commit ?? 'Not reported'}</code></dd><dt>Image</dt><dd><code>{component.digest ?? 'Not reported'}</code></dd></dl></details>}
  </div>;
}

export function CompositionVersions({ environments, sources, onInspect }: { environments: Environment[]; sources: Source[]; onInspect: (environment: string, component: string) => void }) {
  const [params, setParams] = useSearchParams();
  const baselineName = params.get('baseline') ?? environments[0]?.name ?? '';
  const targetName = params.get('compare') ?? environments[1]?.name ?? environments[0]?.name ?? '';
  const focus = params.get('focus') ?? '';
  const choose = (key: 'baseline' | 'compare' | 'focus', value: string) => { const next = new URLSearchParams(params); if (value) next.set(key, value); else next.delete(key); setParams(next, { replace: true }); };
  const swap = () => { const next = new URLSearchParams(params); next.set('baseline', targetName); next.set('compare', baselineName); setParams(next, { replace: true }); };
  const baseline = environments.find(item => item.name === baselineName);
  const target = environments.find(item => item.name === targetName);
  const beforeComponents = new Map(baseline?.components.map(component => [component.name, component]));
  const afterComponents = new Map(target?.components.map(component => [component.name, component]));
  const sourceDetails = new Map(sources.map(source => [source.name, source]));
  const names = [...new Set([...beforeComponents.keys(), ...afterComponents.keys()])].sort();
  const rows = names.map(name => { const before = beforeComponents.get(name); const after = afterComponents.get(name); return { name, before, after, ...compareComponentVersions(before, after) }; });
  const visible = focus ? rows.filter(row => row.name === focus) : rows;
  return <section className="panel composition-versions" aria-label="Environment version comparison">
    <div className="panelhead"><div><p className="eyebrow">Revision intelligence</p><h2>What changes between environments?</h2><p className="small muted">Compare reported source and image identities. Different does not mean newer.</p></div></div>
    <div className="composition-compare-controls">
      <label>Baseline environment<select value={baselineName} onChange={event => choose('baseline', event.target.value)}>{!baseline && <option value={baselineName}>Unavailable selection</option>}{environments.map(item => <option key={item.name}>{item.name}</option>)}</select></label>
      <button className="version-swap" onClick={swap} disabled={!baseline || !target || baselineName === targetName} aria-label="Swap environments" title="Swap baseline and target">⇄</button>
      <label>Target environment<select value={targetName} onChange={event => choose('compare', event.target.value)}>{!target && <option value={targetName}>Unavailable selection</option>}{environments.map(item => <option key={item.name}>{item.name}</option>)}</select></label>
      <div className="composition-compare-counts" aria-label="Comparison totals for all components">{(['different', 'same', 'unknown'] as const).map(state => <span key={state} className={`comparison-${state}`}><b>{rows.filter(row => row.state === state).length}</b> {state}</span>)}</div>
    </div>
    {baseline && target && <div className="version-comparison-focus"><label>Focus component<select value={focus} onChange={event => choose('focus', event.target.value)}><option value="">All components ({rows.length})</option>{focus && !names.includes(focus) && <option value={focus}>Unavailable: {focus}</option>}{names.map(name => <option key={name}>{name}</option>)}</select></label>{focus && <button onClick={() => choose('focus', '')}>Show all components</button>}</div>}
    {!baseline || !target ? <div className="panelbody"><Notice title="Comparison selection unavailable">Choose two reported environments to compare their versions.</Notice></div> : focus && visible.length === 0 ? <div className="panelbody"><Notice title="Focused component unavailable"><code>{focus}</code> is not reported in either selected environment. Choose a reported component or show all components.</Notice></div> : visible.length ? <>
      <p className="version-comparison-caption">{focus ? `Comparing ${focus}. Totals include all ${rows.length} reported components.` : `Comparing ${rows.length} reported components.`} Refs identify exact reported commits; they do not establish ancestry or running workload state.</p>
      <div className="composition-matrix-scroll"><table className="composition-version-matrix"><thead><tr><th scope="col">Component</th><th scope="col">{baseline.name}<small>Baseline</small></th><th scope="col">{target.name}<small>Target</small></th><th scope="col">Comparison</th></tr></thead><tbody>{visible.map(row => {
        const beforeSource = sourceDetails.get(row.before?.source ?? '');
        const afterSource = sourceDetails.get(row.after?.source ?? '');
        const compareUrl = row.source === 'same' ? githubCompareUrl(beforeSource?.url, row.before?.commit, row.after?.commit) : null;
        return <tr key={row.name} data-focused={Boolean(focus)}>
          <th scope="row"><strong>{row.name}</strong>{row.before && <button className="composition-inline-action" onClick={() => onInspect(baseline.name, row.name)}>Inspect baseline →</button>}{row.after && <button className="composition-inline-action" onClick={() => onInspect(target.name, row.name)}>Inspect target →</button>}</th>
          <td><VersionCell component={row.before} environment={baseline.name} source={beforeSource} /></td><td><VersionCell component={row.after} environment={target.name} source={afterSource} /></td>
          <td><span className={`composition-comparison comparison-${row.state}`}>{row.state === 'same' ? 'Same' : row.state === 'different' ? 'Different' : 'Unknown'}</span><small>Commit: {row.commit} · image: {row.digest}</small>
            <ul className="version-row-reasons">{(!row.before || !row.after) && <li>Component is not reported in both environments.</li>}{row.source === 'different' && <li>Source identity differs; commit ancestry cannot be compared.</li>}{row.source === 'unknown' && <li>Source identity is missing.</li>}{row.source === 'same' && row.commit === 'unknown' && <li>Both complete commit identities are needed.</li>}{row.digest === 'unknown' && <li>Both image identities are needed.</li>}{row.commit === 'same' && row.digest === 'different' && <li>The reported commit matches, but the image differs.</li>}</ul>
            {compareUrl && row.commit === 'different' && <a href={compareUrl} target="_blank" rel="noreferrer">Compare in GitHub ↗</a>}
          </td>
        </tr>;
      })}</tbody></table></div>
    </> : <div className="empty"><h3>No component versions reported</h3><p>The controller has not reported components for these environments.</p></div>}
  </section>;
}
