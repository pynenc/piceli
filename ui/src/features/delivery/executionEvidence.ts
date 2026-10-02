import type { ExecutionJournalRecord, JournalAction, JournalEvent, JournalLog, Operation, PlanRecord, ResourceIdentity } from '../../api/generated';

export type ExecutionStep = NonNullable<PlanRecord['steps']>[number];
export type ExecutionJournal = ExecutionJournalRecord & { actions: JournalAction[]; events: JournalEvent[]; logs: JournalLog[] };

export const resourceIdentity = (resource: ResourceIdentity) => JSON.stringify([resource.target_id, resource.api_version, resource.kind, resource.namespace, resource.name]);
export const resourceName = (resource: ResourceIdentity) => `${resource.kind} / ${resource.namespace || '(cluster scope)'} / ${resource.name}`;

export function matchedAction(step: ExecutionStep, action?: JournalAction) {
  return action?.ordinal === step.ordinal && action.operation === step.operation && resourceIdentity(action.resource) === resourceIdentity(step.resource) ? action : undefined;
}

export function matchingRunPlan(operation: Operation, plan?: PlanRecord) {
  return plan && plan.id === operation.plan_id && plan.application_id === operation.application_id && plan.digest === operation.approved_digest ? plan : undefined;
}

export function matchingRunJournal(operation: Operation) {
  const journal = operation.journal;
  return journal && operation.engine_execution_id && journal.execution_id === operation.engine_execution_id ? { ...journal, actions: journal.actions ?? [], events: journal.events ?? [], logs: journal.logs ?? [] } : undefined;
}

export const stateDescription = (state: string) => ({
  planned: 'This action is in the frozen plan; it has not been executed by approving this review.',
  pending: 'The journal has no write recorded for this action yet.',
  intent: 'Write intent was recorded. A completed write is not yet recorded.',
  applied: 'A write is recorded. Readiness has not been recorded for this action.',
  ready: 'The engine recorded this action as ready.',
  failed: 'The engine recorded this action as failed. Completed writes may remain in effect.',
  compensating: 'Compensation has started; its completion is not yet recorded.',
  compensated: 'Compensation was recorded. Review the journal before another attempt.',
  unreported: 'No matching resource receipt is available. The operation outcome cannot establish this resource’s state.',
}[state] ?? 'State reported by the execution journal.');
