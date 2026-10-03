import type { PlanRecord } from '../../api/generated';

export function PlanContext({ plan }: { plan: PlanRecord }) {
  return <ol className="plan-context" aria-label="Source to target">
    <li><span className="plan-context-number" aria-hidden="true">01</span><div><p className="eyebrow">Frozen source</p><strong>{plan.source?.kind ?? (plan.intent === 'rollback' ? 'Archived release' : 'Source not supplied')}</strong>{plan.source ? <><code title={plan.source.revision}>{plan.source.revision}</code><span>{plan.source.entrypoint}</span></> : <span>Source revision not supplied</span>}</div></li>
    <li><span className="plan-context-number" aria-hidden="true">02</span><div><p className="eyebrow">Exact plan</p><strong>{plan.intent === 'rollback' ? 'Rollback review' : 'Deployment review'}</strong><code title={plan.id}>{plan.id}</code><span>{plan.diffs.length} resource {plan.diffs.length === 1 ? 'entry' : 'entries'} · {plan.authorization === 'policy' ? 'Policy authorization' : 'Manual authorization'}</span></div></li>
    <li><span className="plan-context-number" aria-hidden="true">03</span><div><p className="eyebrow">Deployment target</p><strong>{plan.target.name}</strong><code>{plan.target.namespace || 'Cluster scoped'}</code><span>Target identity: {plan.target.id}</span></div></li>
  </ol>;
}

export function DeliveryProgress({ step }: { step: 1 | 2 | 3 }) {
  return <ol className="delivery-flow" aria-label="Deployment approval flow">{[
    ['Resolve source', 'Approve definition evaluation'],
    ['Review changes', 'Approve the exact plan'],
    ['Follow execution', 'Inspect outcomes and evidence'],
  ].map(([title, detail], index) => <li key={title} aria-current={step === index + 1 ? 'step' : undefined}><span aria-hidden="true">{String(index + 1).padStart(2, '0')}</span><div><strong>{title}</strong><small>{detail}</small></div></li>)}</ol>;
}
