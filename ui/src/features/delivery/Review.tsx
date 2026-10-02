import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useExpired } from './useExpired';
import type { EvaluationPreview, PlanRecord } from '../../api/generated';
import { applicationPath } from '../../api/client';
import { Badge, formatTime, Notice } from '../../components/State';
import { DiffWorkbench } from './DiffWorkbench';
import { PlanContext } from './PlanContext';
import { PlanExecutionFlow } from './PlanExecutionFlow';
import './delivery.css';

export function PreviewApproval({ preview, pending, retry = false, onApprove }: { preview: EvaluationPreview; pending: boolean; retry?: boolean; onApprove: () => void }) {
  const expired = useExpired(preview.expires_at);
  return <section className="panel review-panel delivery-review">
    <div className="panelhead"><div><p className="eyebrow">01 / Source execution preview</p><h2>Review source evaluation</h2></div><Badge value={preview.intent} /></div>
    <div className="delivery-review-grid">
      <div className="delivery-evidence"><p className="subtitle">Evaluation runs the registered definition to produce a deployment plan. Review the frozen source and execution requirements before authorizing it.</p>
        <dl className="facts"><dt>Source</dt><dd>{preview.source.kind}</dd><dt>Revision</dt><dd><code>{preview.source.revision}</code></dd><dt>Entrypoint</dt><dd><code>{preview.source.entrypoint}</code></dd></dl>
        {preview.warnings?.map((warning, i) => <Notice title="Execution requirement" key={i}>{warning}</Notice>)}
        <details><summary>Included files ({preview.files.length})</summary><ul className="file-list">{preview.files.map(file => <li key={file}><code>{file}</code></li>)}</ul></details>
        <details><summary>Limits and authorization identity</summary><dl className="facts">{Object.entries(preview.limits).map(([name, value]) => <div className="fact-pair" key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl><dl className="facts"><dt>Preview digest</dt><dd><code>{preview.digest}</code></dd><dt>Renderer digest</dt><dd><code>{preview.renderer_digest}</code></dd><dt>Input digest</dt><dd><code>{preview.input_digest}</code></dd></dl></details>
      </div>
      <aside className="delivery-decision" aria-label="Source evaluation approval"><p className="eyebrow">Approval boundary</p><h3>Evaluate this source</h3>
        <dl className="facts"><dt>Intent</dt><dd>{preview.intent === 'rollback' ? `Rollback to ${preview.release}` : 'Deploy'}</dd><dt>Expires</dt><dd>{formatTime(preview.expires_at)}</dd></dl>
        {expired && <Notice title="Evaluation preview expired">Prepare a new preview before approving source execution.</Notice>}
        <div className="review-footer"><p className="small muted">This approval authorizes source evaluation. Deployment requires review of the resulting plan.</p><button className="primary" disabled={pending || expired} onClick={onApprove}>{pending ? 'Requesting evaluation…' : retry ? 'Retry same evaluation request' : 'Approve source evaluation'}</button></div>
      </aside>
    </div>
  </section>;
}

export function PlanReview({ plan, pending, allowed, reason, retry = false, onApprove }: { plan: PlanRecord; pending: boolean; allowed: boolean; retry?: boolean; reason?: string | null; onApprove: () => void }) {
  const [confirmedIdentity, setConfirmedIdentity] = useState<string | null>(null);
  const identity = `${plan.id}:${plan.digest}`;
  const confirmed = confirmedIdentity === identity;
  const expired = useExpired(plan.expires_at);
  const unsupported = plan.plan_kind === 'pipeline-preview';
  return <section className="panel review-panel delivery-review">
    <div className="panelhead"><div><p className="eyebrow">02 / Exact deployment plan</p><h2>{plan.intent === 'rollback' ? 'Review rollback' : 'Review deployment'}</h2></div><div className="plan-review-links"><Link to={`${applicationPath(plan.application_id)}/activity?history=revisions&compareTo=${encodeURIComponent(plan.id)}`}>Compare revision</Link><Badge value={plan.intent} /></div></div>
    <PlanContext plan={plan} />
    <div className="delivery-impact" aria-label="Plan impact">{Object.entries(plan.summary).map(([name, count]) => <div key={name}><strong>{count}</strong><span>{name}</span></div>)}<div className="delivery-impact-target"><span>Deployment target</span><strong>{plan.target.name} / {plan.target.namespace}</strong></div></div>
    <div className="delivery-review-grid">
      <div className="delivery-evidence">
        <PlanExecutionFlow key={identity} steps={plan.steps} />
        <div className="delivery-section-heading"><div><p className="eyebrow">Change surface</p><h3>Field changes</h3></div><span className="small muted">{plan.diffs.length} resource {plan.diffs.length === 1 ? 'diff' : 'diffs'}</span></div>
        {plan.intent === 'rollback' && <Notice title="Rollback is a new deployment">This plan applies the archived release against current live state. It does not restore application data.</Notice>}
        {plan.warnings?.map((warning, i) => <Notice title="Plan warning" key={i}>{warning}</Notice>)}
        {plan.diffs.length === 0 ? <p className="muted small">This plan reports no field changes. Review any remaining actions and checks below.</p> : <DiffWorkbench key={identity} diffs={plan.diffs} />}
        <details><summary>Actions and checks</summary><h4>Actions</h4><pre>{JSON.stringify(plan.actions ?? [], null, 2)}</pre><h4>Checks</h4><pre>{JSON.stringify(plan.checks ?? {}, null, 2)}</pre></details>
        <details><summary>Plan identity and preconditions</summary><dl className="facts"><dt>Plan ID</dt><dd><code>{plan.id}</code></dd><dt>Engine digest</dt><dd><code>{plan.engine_digest ?? 'Not supplied'}</code></dd><dt>Preconditions</dt><dd><code>{plan.precondition_digest ?? 'Not supplied'}</code></dd>{plan.policy_digest && <><dt>Policy digest</dt><dd><code>{plan.policy_digest}</code></dd></>}</dl></details>
      </div>
      <aside className="delivery-decision" aria-label="Deployment approval"><p className="eyebrow">Approval boundary</p><h3>What this approval covers</h3>
        <dl className="facts"><dt>Target</dt><dd><strong>{plan.target.name} / {plan.target.namespace}</strong></dd><dt>Target ID</dt><dd><code>{plan.target.id}</code></dd><dt>Source revision</dt><dd><code>{plan.source?.revision ?? 'Archived release'}</code></dd><dt>Release</dt><dd>{plan.release}</dd><dt>Authorization</dt><dd>{plan.authorization === 'policy' ? 'Reviewed policy' : 'Manual approval'}</dd><dt>Expires</dt><dd>{formatTime(plan.expires_at)}</dd></dl>
        <div className="delivery-digest"><span>Approved digest</span><code>{plan.digest}</code></div>
        {expired && <Notice title="Plan expired">Prepare and review a new plan. This plan cannot be approved.</Notice>}{unsupported && <Notice title="Materialized plan required">Build output must be materialized and reviewed before deployment is available.</Notice>}{!allowed && <Notice title="Deployment unavailable">{reason ?? 'This session cannot deploy this application.'}</Notice>}
        <label className="confirmation"><input type="checkbox" checked={confirmed} onChange={e => setConfirmedIdentity(e.target.checked ? identity : null)} />I reviewed the changes, source revision and target {plan.target.name} / {plan.target.namespace}.</label>
        <div className="review-footer"><p className="small muted">Approval applies only to this exact plan. Changed preconditions require a new review.</p><button className="primary" disabled={!confirmed || !allowed || expired || unsupported || pending} onClick={onApprove}>{pending ? 'Requesting deployment…' : retry ? 'Retry same deployment request' : `${plan.intent === 'rollback' ? 'Roll back' : 'Deploy'} to ${plan.target.name} / ${plan.target.namespace}`}</button></div>
      </aside>
    </div>
  </section>;
}
