import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { api, applicationPath } from '../../api/client';
import type { Application, EvaluationPreview, PlanRecord, PlanRequest } from '../../api/generated';
import { Badge, Failure, formatTime, Loading, Notice } from '../../components/State';
import { PlanReview, PreviewApproval } from './Review';
import { RequestFailure } from './RequestFailure';
import { requestKey } from './requests';
import { RunView } from './RunView';
import { useDeliveryEvents } from './useDeliveryEvents';

const active = (state?: string) => ['queued', 'running', 'cancelling'].includes(state ?? '');

export function Delivery({ application }: { application: Application }) {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const intent = params.get('intent') === 'rollback' ? 'rollback' : 'deploy';
  const [release, setRelease] = useState(params.get('release') ?? '');
  const evaluationId = params.get('evaluation');
  const planId = params.get('plan');
  const releases = useQuery({ queryKey: ['releases', application.id], queryFn: ({ signal }) => api.releases(application.id, signal), enabled: intent === 'rollback' });
  const preview = useMutation({ mutationFn: (body: PlanRequest) => api.preview(application.id, body), retry: false });
  const evaluationAdmission = useMutation({ mutationFn: (record: EvaluationPreview) => api.evaluate(application.id, { preview_id: record.id, approved_digest: record.digest, idempotency_key: requestKey('evaluate', `${record.id}:${record.digest}`) }), retry: false, onSuccess: (record, approved) => { setParams({ evaluation: record.id, intent: approved.intent, ...(approved.release ? { release: approved.release } : {}) }); } });
  const evaluation = useQuery({ queryKey: ['evaluation', evaluationId], queryFn: ({ signal }) => api.evaluation(evaluationId!, signal), enabled: Boolean(evaluationId) && !planId, refetchInterval: query => active(query.state.data?.state) ? 1000 : false });
  useEffect(() => {
    if (!planId && evaluation.data?.state === 'succeeded' && evaluation.data.plan_id) setParams(previous => { const next = new URLSearchParams(previous); next.delete('evaluation'); next.set('plan', evaluation.data!.plan_id!); return next; }, { replace: true });
  }, [evaluation.data, planId, setParams]);
  const plan = useQuery({ queryKey: ['plan', planId], queryFn: ({ signal }) => api.plan(planId!, signal), enabled: Boolean(planId), refetchInterval: false });
  const admission = useMutation({ mutationFn: (record: PlanRecord) => api.deploy(application.id, { plan_id: record.id, approved_digest: record.digest, idempotency_key: requestKey('deploy', `${record.id}:${record.digest}`) }), retry: false, onSuccess: operation => { void client.invalidateQueries({ queryKey: ['operations', application.id] }); navigate(`/runs/${encodeURIComponent(operation.id)}`); } });
  const canEvaluate = application.capabilities?.evaluate;
  const canRollback = application.capabilities?.rollback;
  const preparing = preview.isPending || evaluationAdmission.isPending || admission.isPending;
  function freshReview() { const nextIntent = plan.data?.intent ?? preview.data?.intent ?? intent; preview.reset(); evaluationAdmission.reset(); admission.reset(); setParams(nextIntent === 'rollback' ? { intent: 'rollback' } : {}); }
  return <><div className="delivery-toolbar"><h2>Changes</h2>{(planId || evaluationId || preview.data) && <button disabled={preparing} onClick={freshReview}>Prepare a new review</button>}</div>
    {preview.isError && <RequestFailure error={preview.error} />}{evaluationAdmission.isError && <RequestFailure error={evaluationAdmission.error} />}{admission.isError && <RequestFailure error={admission.error} />}
    {planId ? <>{plan.isPending && <Loading text="Loading exact plan…" />}{plan.isError && <Failure error={plan.error} retry={() => void plan.refetch()} />}{plan.data && <PlanReview key={`${plan.data.id}:${plan.data.digest}`} plan={plan.data} pending={admission.isPending} retry={admission.isError} allowed={application.capabilities?.deploy?.allowed === true && plan.data.application_id === application.id} reason={application.capabilities?.deploy?.reason} onApprove={() => admission.mutate(plan.data)} />}</> : evaluationId ? <>{evaluation.isPending && <Loading text="Loading evaluation…" />}{evaluation.isError && <Failure error={evaluation.error} retry={() => void evaluation.refetch()} />}{evaluation.data && <section className="panel"><div className="evaluation-status"><Badge value={evaluation.data.state} /><div><strong>Source evaluation</strong><p className="small muted"><code>{evaluation.data.id}</code></p></div></div>{['failed', 'interrupted'].includes(evaluation.data.state) && <div className="panelbody"><Notice title="Evaluation did not produce a plan" danger>{evaluation.data.error_code ?? 'Inspect the registered source and execution requirements before preparing another review.'}</Notice></div>}{evaluation.data.state === 'succeeded' && !evaluation.data.plan_id && <div className="panelbody"><Notice title="Plan evidence unavailable">The evaluation reports completion but has no plan identity. Deployment is unavailable.</Notice></div>}<p className="panelbody small muted">This evaluation is tracked by the service. Reloading this page reconnects to the same evaluation.</p></section>}</> : preview.data ? <PreviewApproval key={preview.data.id} preview={preview.data} pending={evaluationAdmission.isPending} retry={evaluationAdmission.isError} onApprove={() => evaluationAdmission.mutate(preview.data)} /> : <section className="panel"><div className="panelbody"><h3>Prepare deployment review</h3><p className="subtitle">Resolve the registered definition to a frozen source, approve evaluation, then review the actual deployment plan.</p><div className="intent-form"><label>Deployment intent<select value={intent} onChange={e => setParams(e.target.value === 'rollback' ? { intent: 'rollback' } : {})}><option value="deploy">Deploy registered source</option><option value="rollback">Roll back an archived release</option></select></label>{intent === 'rollback' && <label>Archived release<select value={release} onChange={e => setRelease(e.target.value)}><option value="">Select a release</option>{releases.data?.items.map(record => <option key={record.name} value={record.name} disabled={record.capabilities?.rollback?.allowed === false}>{record.name} · {record.state}</option>)}</select></label>}<button className="primary" disabled={preparing || !canEvaluate?.allowed || (intent === 'rollback' && (!release || !canRollback?.allowed))} onClick={() => preview.mutate({ intent, ...(intent === 'rollback' ? { release } : {}) })}>{preview.isPending ? 'Preparing preview…' : 'Prepare source preview'}</button></div>
      {!canEvaluate?.allowed && <Notice title="Source evaluation unavailable">{canEvaluate?.reason ?? 'This application cannot evaluate a source in this session.'}</Notice>}{intent === 'rollback' && <>{releases.isPending && <Loading text="Loading archived releases…" />}{releases.isError && <Failure error={releases.error} retry={() => void releases.refetch()} />}{releases.data?.items.length === 0 && <Notice title="No archived releases">There is no release available to plan a rollback.</Notice>}{!canRollback?.allowed && <Notice title="Rollback unavailable">{canRollback?.reason ?? 'No rollback capability is available for this application.'}</Notice>}<p className="small muted">Rollback creates a new plan and operation. Selecting an archive does not change the deployment.</p></>}
    </div></section>}
  </>;
}

export function Activity({ applicationId }: { applicationId: string }) {
  const [streamConnected, setStreamConnected] = useState(false);
  const operations = useQuery({ queryKey: ['operations', applicationId], queryFn: ({ signal }) => api.operations(applicationId, signal), refetchInterval: streamConnected ? false : 3000 });
  const { connected, gap } = useDeliveryEvents(applicationId, operations.data?.cursor);
  useEffect(() => setStreamConnected(connected), [connected]);
  return <><div className="delivery-toolbar"><h2>Activity</h2><button disabled={operations.isFetching} onClick={() => void operations.refetch()}>Refresh runs</button></div>{gap && <Notice title="Event history expired">Some live updates were missed. The current activity snapshot was fetched again.</Notice>}{operations.data && !connected && <Notice title="Live updates disconnected">The activity list is refreshing while the event stream reconnects.</Notice>}{operations.isPending && <Loading text="Loading deployment history…" />}{operations.isError && <Failure error={operations.error} retry={() => void operations.refetch()} />}{operations.data && <section className="panel">{operations.data.items.length === 0 ? <div className="empty"><h3>No recorded operations</h3><p>Operations submitted to this service will appear here.</p></div> : operations.data.items.map(operation => <article className="operation-row" key={operation.id}><div><Link to={`/runs/${encodeURIComponent(operation.id)}`}>{operation.engine_release ?? operation.id}</Link><p className="small muted">{operation.trigger.toUpperCase()} · {operation.actor} · {formatTime(operation.created_at)}</p><p className="small muted">Attempt {operation.attempt ?? 1}{operation.recovery_of ? ` · Recovery of ${operation.recovery_of}` : ''}</p></div><div className="operation-result"><Badge value={operation.state} />{operation.error_code && <code>{operation.error_code}</code>}</div></article>)}{operations.data.next_page && <Notice title="Partial history">Additional history is available beyond this page.</Notice>}</section>}</>;
}

export function Run() {
  const { operationId = '' } = useParams();
  const client = useQueryClient();
  const navigate = useNavigate();
  const query = useQuery({ queryKey: ['operation', operationId], queryFn: ({ signal }) => api.operation(operationId, signal), refetchInterval: current => active(current.state.data?.state) ? 1000 : false });
  const operation = query.data;
  const plan = useQuery({ queryKey: ['plan', operation?.plan_id], queryFn: ({ signal }) => api.plan(operation!.plan_id, signal), enabled: Boolean(operation?.plan_id), refetchInterval: false });
  const resume = useMutation({ mutationFn: () => api.resume(operationId, { approved_digest: operation!.approved_digest, idempotency_key: requestKey('resume', `${operationId}:${operation!.approved_digest}`) }), retry: false, onSuccess: result => { void client.invalidateQueries({ queryKey: ['operations', result.application_id] }); navigate(`/runs/${encodeURIComponent(result.id)}`); } });
  const cancel = useMutation({ mutationFn: () => api.cancel(operationId, { idempotency_key: requestKey('cancel', operationId) }), retry: false, onSuccess: result => { client.setQueryData(['operation', operationId], result); void client.invalidateQueries({ queryKey: ['operations', result.application_id] }); } });
  // Each new operation route owns independent mutation results and confirmation state.
  return <>{operation && <Link className="back-link" to={`${applicationPath(operation.application_id)}/activity`}>← Application activity</Link>}{query.isPending && <Loading text="Loading deployment run…" />}{query.isError && <Failure error={query.error} retry={() => void query.refetch()} />}{resume.isError && <RequestFailure error={resume.error} />}{cancel.isError && <RequestFailure error={cancel.error} />}{operation && <RunView key={operation.id} operation={operation} plan={plan.data} pending={resume.isPending || cancel.isPending} onResume={() => resume.mutate()} onCancel={() => cancel.mutate()} />}</>;
}
