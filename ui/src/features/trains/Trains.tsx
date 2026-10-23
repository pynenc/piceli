import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { Badge, Failure, formatTime, Loading, Notice } from '../../components/State';
import '../control/operational-harbor.css';

type StepLive = { trial: number | null; name: string; kind?: string | null; host?: string | null; started?: string | null; elapsed?: number | null; tail?: string | null };
type StepDone = { trial: number | null; name: string; verdict: string; duration?: number | null; host?: string | null };
type Current = {
  run: string; started?: string | null; elapsed: number; phase: string; branches: string[]; verdicts: Record<string, string>;
  trial: { id: number; branches: string[]; base?: string | null; started?: string | null } | null; step: StepLive | null; done: StepDone[];
};
type RunRow = {
  run: string; state: string; ok: boolean | null; started?: string | null; duration?: number | null; host?: string | null; head?: string | null;
  branches: { branch: string; verdict?: string; step?: string; reason?: string }[]; trials: number; steps: Record<string, number>;
  tag?: string | null; promote: string; push: string; flaky: number; reason?: string | null; queue?: string | null;
};
type QueueRow = { id: string; state: string; position?: number | null; branches: string[]; promote: boolean; push: boolean; queued_at?: string | null; started_at?: string | null; run?: string | null; resumed?: number | null };
type Trend = { step: string; samples: number; median: number; last: number; last_run: string; ratio: number | null; trend: string };
type Flaky = { key: string; test: string; target: string; count: number; first_seen?: string | null; last_seen?: string | null; last_run?: string | null; last_branches: string[]; last_tail: string };
export type TrainView = { train: string; state: 'running' | 'idle'; current: Current | null; queue?: QueueRow[]; runs: RunRow[]; trends: Trend[]; flaky: Flaky[] };
export type TrainsStatus = { schema: string; trains: TrainView[] };
type Producer = { train: string; run: string; tag?: string | null; head?: string | null; started?: string | null; joined: string[]; pushed: boolean };
type DeployedRow = { kind: string; name: string; where?: string | null; state: string; revision: { commit?: string | null; tag?: string | null }; pinned?: string | null; train: Producer | null; matched: string | null };
export type TrainsDeployed = { configured: boolean; rows: DeployedRow[]; reason?: string; generated_at?: string | null };

export const seconds = (value?: number | null) => value == null ? '—' : value >= 5400 ? `${(value / 3600).toFixed(1)} h` : value >= 90 ? `${(value / 60).toFixed(1)} min` : value >= 10 ? `${Math.round(value)} s` : `${value.toFixed(1)} s`;
const short = (value?: string | null) => value ? value.slice(0, 12) : '—';
const STEP_HIDDEN = new Set(['merge', 'spec']);

function CurrentRun({ train, current }: { train: string; current: Current }) {
  const step = current.step;
  return <section className="panel control-card" aria-label={`Running ${train}`}>
    <div className="panelhead"><h2>Running: {current.run}</h2><span className="small muted">{current.phase} · {seconds(current.elapsed)} since {formatTime(current.started)}</span></div>
    <div className="panelbody">
      <ul className="control-workloads" aria-label="Branches">{current.branches.map(branch => <li key={branch}><strong>{branch}</strong><span><Badge value={current.verdicts[branch] ?? 'pending'} /></span></li>)}</ul>
      {current.trial && <p className="small">Trial {current.trial.id}: {current.trial.branches.join(' ')}{current.trial.base ? ` on ${short(current.trial.base)}` : ''}</p>}
      <ol className="control-workloads" aria-label="Steps">
        {current.done.filter(item => !STEP_HIDDEN.has(item.name)).map(item => <li key={`${item.trial}-${item.name}`}><strong>{item.name}</strong><span><Badge value={item.verdict} /> {seconds(item.duration)} on {item.host ?? 'local'}</span></li>)}
        {step && <li key="live"><strong>{step.name}</strong><span><Badge value="running" /> {seconds(step.elapsed)} on {step.host ?? 'local'} · started {formatTime(step.started)}</span></li>}
      </ol>
      {step && <pre className="log-output" aria-label={`Log of ${step.name}`}>{step.tail || 'No output yet.'}</pre>}
    </div>
  </section>;
}

function Runs({ runs }: { runs: RunRow[] }) {
  return <section className="panel control-card" aria-label="Recent runs"><div className="panelhead"><h2>Recent runs</h2></div><div className="panelbody">
    {runs.length ? <ul className="control-workloads">{runs.map(run => <li key={run.run}>
      <strong>{run.run} <Badge value={run.state} />{run.reason ? <span className="small muted"> [{run.reason}]</span> : null}</strong>
      <span>{seconds(run.duration)} · {run.trials} trial{run.trials === 1 ? '' : 's'}{run.tag ? ` · ${run.tag}` : ''}{run.push === 'pushed' ? ' · pushed' : ''} · {run.branches.map(b => `${b.branch} ${b.verdict ?? ''}${b.verdict === 'ejected' && b.step ? ` (${b.step})` : ''}`).join(', ')}</span>
      <span className="small muted">{Object.entries(run.steps).filter(([name]) => !STEP_HIDDEN.has(name)).map(([name, value]) => `${name} ${seconds(value)}`).join(' · ') || 'No step ran'} · {formatTime(run.started)}</span>
    </li>)}</ul> : <p className="small muted">No run has finished yet.</p>}
  </div></section>;
}

function Queue({ train, queue }: { train: string; queue: QueueRow[] }) {
  return <section className="panel control-card" aria-label={`Queue of ${train}`}><div className="panelhead"><h2>Queue</h2><span className="small muted">Served in order when the running train ends</span></div><div className="panelbody">
    {queue.length ? <ol className="control-workloads">{queue.map(row => <li key={row.id}>
      <strong>{row.position ? `#${row.position} ` : ''}{row.branches.join(' ')}</strong>
      <span>{row.state !== 'waiting' ? <Badge value={row.state} /> : null} {[row.promote ? '--promote' : '', row.push ? '--push' : ''].filter(Boolean).join(' ') || 'test only'} · queued {formatTime(row.queued_at)}{row.resumed ? ` · resumed ${row.resumed}×` : ''}</span>
      <span className="small muted">{row.id}</span>
    </li>)}</ol> : <p className="small muted">Nothing is waiting.</p>}
  </div></section>;
}

function Trends({ trends }: { trends: Trend[] }) {
  return <section className="panel control-card" aria-label="Step trends"><div className="panelhead"><h2>Step durations</h2><span className="small muted">Median of the last executions; the last one against it</span></div><div className="panelbody">
    {trends.length ? <ul className="control-workloads">{trends.map(row => <li key={row.step}>
      <strong>{row.step}</strong>
      <span>median {seconds(row.median)} · last {seconds(row.last)} <Badge value={row.trend} /> <span className="small muted">({row.samples} runs{row.ratio != null ? `, ${Math.round(row.ratio * 100)} %` : ''})</span></span>
    </li>)}</ul> : <p className="small muted">No step has run yet.</p>}
  </div></section>;
}

function FlakyTests({ flaky }: { flaky: Flaky[] }) {
  return <section className="panel control-card" aria-label="Flaky tests"><div className="panelhead"><h2>Flaky tests</h2></div><div className="panelbody">
    {flaky.length ? <ul className="control-workloads">{flaky.map(row => <li key={row.key}>
      <strong>{row.test} <span className="small muted">×{row.count}</span></strong>
      <span>{row.target} · last {formatTime(row.last_seen)}{row.last_branches.length ? ` with ${row.last_branches.join(' ')}` : ''}</span>
      {row.last_tail && <details style={{ gridColumn: '1 / -1' }}><summary className="small">Last first failure</summary><pre className="log-output">{row.last_tail}</pre></details>}
    </li>)}</ul> : <p className="small muted">No flaky test seen.</p>}
  </div></section>;
}

function Deployed() {
  const query = useQuery({ queryKey: ['trains-deployed'], queryFn: ({ signal }) => api.trainsDeployed(signal).then(value => value as unknown as TrainsDeployed), refetchInterval: 60000 });
  const body = query.data;
  if (!body?.configured) return query.isError ? <Failure error={query.error} retry={() => void query.refetch()} /> : null;
  return <section className="panel control-card" aria-label="Deployed by train"><div className="panelhead"><h2>Deployed by train</h2><span className="small muted">{body.generated_at ? `Read ${formatTime(body.generated_at)}` : ''}</span></div><div className="panelbody">
    {body.reason && <Notice title="Targets not read">{body.reason}</Notice>}
    {body.rows.length ? <ul className="control-workloads">{body.rows.map(row => <li key={`${row.kind}-${row.name}`}>
      <strong>{row.name} <Badge value={row.state} /></strong>
      <span>{row.kind === 'nix-host' ? 'NixHost' : 'Environment'}{row.where ? ` · ${row.where}` : ''} · {row.revision.tag ?? short(row.revision.commit)}</span>
      <span className="small">{row.train ? `Train ${row.train.train}, run ${row.train.run}${row.train.tag ? ` (${row.train.tag})` : ''}: ${row.train.joined.join(', ') || 'no branch'} · matched by ${row.matched}` : 'No train run produced this'}</span>
    </li>)}</ul> : <p className="small muted">No target declared.</p>}
  </div></section>;
}

export function Trains() {
  const query = useQuery({ queryKey: ['trains'], queryFn: ({ signal }) => api.trains(signal).then(value => value as unknown as TrainsStatus), refetchInterval: 2000 });
  const status = query.data;
  const trains = status?.trains ?? [];
  const running = trains.filter(train => train.current).length;
  return <div className="cluster-overview">
    <div className="heading detail-heading"><div><p className="eyebrow">Delivery</p><h1>Trains</h1><p className="subtitle">Merge trains on this machine: the run in progress, recent runs, step durations, flaky tests and what each train produced. Runs start on the command line (<code>piceli train</code>).</p></div><button onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh</button></div>
    {query.isPending && <Loading text="Loading trains…" />}
    {query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}
    {status && trains.length === 0 && <Notice title="No train has run yet">Declare a <code>piceli.train.Train</code> and run <code>piceli train APP --branches …</code>.</Notice>}
    {trains.length > 0 && <div className="statusbar" aria-label="Trains state">
      <div className="statusitem"><span className="statuslabel">Trains</span><strong>{trains.length}</strong></div>
      <div className="statusitem"><span className="statuslabel">Running</span><strong>{running}</strong></div>
      <div className="statusitem"><span className="statuslabel">Queued</span><strong>{trains.reduce((sum, train) => sum + (train.queue?.length ?? 0), 0)}</strong></div>
    </div>}
    {trains.map(train => <section key={train.train} aria-label={`Train ${train.train}`}>
      <h2>{train.train} <Badge value={train.state} /></h2>
      {train.current ? <CurrentRun train={train.train} current={train.current} /> : <p className="small muted">No run in progress.</p>}
      {(train.queue?.length ?? 0) > 0 && <Queue train={train.train} queue={train.queue ?? []} />}
      <Runs runs={train.runs} />
      <Trends trends={train.trends} />
      <FlakyTests flaky={train.flaky} />
    </section>)}
    {status && <Deployed />}
  </div>;
}
