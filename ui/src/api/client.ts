import createClient from 'openapi-fetch';
import type { paths } from './openapi';
import type { ServiceError, PlanRequest, EvaluationRequest, OperationRequest, RecoveryRequest, CancelRequest, AccessStartRequest, RemoteAccessStartRequest, EnvironmentActionRequest, GitOpsApprovalRequest, GitOpsPromotionRequest, ClusterBuildPlanRequest, ClusterBuildOperationRequest, CompositionSyncRequest, NamedEnvironmentApprovalRequest, NamedEnvironmentPromotionRequest, ProfileSwitchRequest, ProfileScopeRequest } from './generated';

export class ApiError extends Error {
  constructor(public status: number, public detail: ServiceError) { super(detail.message); }
}
export function basePath(): string {
  // The Python server injects the configured prefix into the document's base.
  return new URL(document.querySelector('base')?.href ?? '/', window.location.origin).pathname.replace(/\/$/, '');
}
function service() {
  const client = createClient<paths>({ baseUrl: `${window.location.origin}${basePath()}`, credentials: 'same-origin', fetch: request => globalThis.fetch(request), headers: { Accept: 'application/json' } });
  client.use({ onRequest({ request }) {
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method)) {
      // The server names its own CSRF cookie: other local UIs' cookies for
      // the same host are visible here too.
      const name = document.querySelector('meta[name="piceli-csrf-cookie"]')?.getAttribute('content');
      const token = name ? document.cookie.split(';').map(value => value.trim()).find(value => value.startsWith(`${name}=`))?.slice(name.length + 1) : undefined;
      if (token) request.headers.set('X-Piceli-CSRF', decodeURIComponent(token));
    }
    return request;
  }, async onResponse({ response }) {
    if (response.ok) return response;
    let detail: ServiceError = { code: 'request-failed', message: 'The service could not complete this request.', correlation_id: '' };
    try {
      const body = await response.clone().json();
      if (typeof body.code === 'string' && typeof body.message === 'string') detail = body;
    } catch { /* Never expose an unstructured proxy response. */ }
    throw new ApiError(response.status, detail);
  } });
  return client;
}
function data<T>(result: { data?: T }): T {
  if (result.data === undefined) throw new Error('The service returned no data.');
  return result.data;
}
// Endpoint names, parameters and return values are checked against actual OpenAPI.
export const api = {
  capabilities: (signal?: AbortSignal) => service().GET('/api/v1/capabilities', { signal }).then(data),
  applications: (cursor: string | null, signal?: AbortSignal) => service().GET('/api/v1/applications', { params: { query: { cursor } }, signal }).then(data),
  application: (application_id: string, signal?: AbortSignal) => service().GET('/api/v1/applications/{application_id}', { params: { path: { application_id } }, signal }).then(data),
  resources: (application_id: string, cursor: string | null, signal?: AbortSignal) => service().GET('/api/v1/applications/{application_id}/resources', { params: { path: { application_id }, query: { cursor } }, signal }).then(data),
  resource: (application_id: string, resource_id: string, signal?: AbortSignal) => service().GET('/api/v1/applications/{application_id}/resources/{resource_id}', { params: { path: { application_id, resource_id } }, signal }).then(data),
  logSources: (application_id: string, resource_id: string, resource_uid: string, signal?: AbortSignal) => service().GET('/api/v1/applications/{application_id}/resources/{resource_id}/log-sources', { params: { path: { application_id, resource_id }, query: { resource_uid } }, signal }).then(data),
  logs: (application_id: string, resource_id: string, resource_uid: string, pod_name: string, pod_uid: string, container: string, previous: boolean, tail_lines: number, signal?: AbortSignal) => service().GET('/api/v1/applications/{application_id}/resources/{resource_id}/logs', { params: { path: { application_id, resource_id }, query: { resource_uid, pod_name, pod_uid, container, previous, tail_lines } }, signal }).then(data),
  preview: (application_id: string, body: PlanRequest) => service().POST('/api/v1/applications/{application_id}/evaluation-preview', { params: { path: { application_id } }, body: { ...body, intent: body.intent ?? 'deploy' } }).then(data),
  evaluate: (application_id: string, body: EvaluationRequest) => service().POST('/api/v1/applications/{application_id}/evaluations', { params: { path: { application_id } }, body }).then(data),
  evaluation: (evaluation_id: string, signal?: AbortSignal) => service().GET('/api/v1/evaluations/{evaluation_id}', { params: { path: { evaluation_id } }, signal }).then(data),
  plan: (plan_id: string, signal?: AbortSignal) => service().GET('/api/v1/plans/{plan_id}', { params: { path: { plan_id } }, signal }).then(data),
  plans: (application_id: string, page?: string, signal?: AbortSignal) => service().GET('/api/v1/applications/{application_id}/plans', { params: { path: { application_id }, query: { page } }, signal }).then(data),
  deploy: (application_id: string, body: OperationRequest) => service().POST('/api/v1/applications/{application_id}/operations', { params: { path: { application_id } }, body }).then(data),
  operations: (application_id: string, signal?: AbortSignal) => service().GET('/api/v1/applications/{application_id}/operations', { params: { path: { application_id } }, signal }).then(data),
  operation: (operation_id: string, signal?: AbortSignal) => service().GET('/api/v1/operations/{operation_id}', { params: { path: { operation_id } }, signal }).then(data),
  releases: (application_id: string, signal?: AbortSignal) => service().GET('/api/v1/applications/{application_id}/releases', { params: { path: { application_id } }, signal }).then(data),
  resume: (operation_id: string, body: RecoveryRequest) => service().POST('/api/v1/operations/{operation_id}/resume', { params: { path: { operation_id } }, body }).then(data),
  cancel: (operation_id: string, body: CancelRequest) => service().POST('/api/v1/operations/{operation_id}/cancel', { params: { path: { operation_id } }, body }).then(data),
  accessSessions: (application_id: string, signal?: AbortSignal) => service().GET('/api/v1/applications/{application_id}/access-sessions', { params: { path: { application_id } }, signal }).then(data),
  startAccess: (application_id: string, body: AccessStartRequest) => service().POST('/api/v1/applications/{application_id}/access-sessions', { params: { path: { application_id } }, body: { ...body, duration_seconds: body.duration_seconds ?? 900 } }).then(data),
  stopAccess: (application_id: string, session_id: string) => service().DELETE('/api/v1/applications/{application_id}/access-sessions/{session_id}', { params: { path: { application_id, session_id } } }).then(data),
  remoteAccessSessions: (application_id: string, signal?: AbortSignal) => service().GET('/api/v1/applications/{application_id}/remote-access', { params: { path: { application_id } }, signal }).then(data),
  issueRemoteAccess: (application_id: string, body: RemoteAccessStartRequest) => service().POST('/api/v1/applications/{application_id}/remote-access', { params: { path: { application_id } }, body: { ...body, duration_seconds: body.duration_seconds ?? 900 } }).then(data),
  stopRemoteAccess: (application_id: string, ticket_id: string) => service().DELETE('/api/v1/applications/{application_id}/remote-access/{ticket_id}', { params: { path: { application_id, ticket_id } } }).then(data),
  environments: (signal?: AbortSignal) => service().GET('/api/v1/environments', { signal }).then(data),
  environmentAction: (body: EnvironmentActionRequest) => service().POST('/api/v1/environments/actions', { body }).then(data),
  gitops: (signal?: AbortSignal) => service().GET('/api/v1/gitops', { signal }).then(data),
  approveGitops: (body: GitOpsApprovalRequest) => service().POST('/api/v1/gitops/approvals', { body }).then(data),
  promoteGitops: (body: GitOpsPromotionRequest) => service().POST('/api/v1/gitops/promotions', { body }).then(data),
  pipelinePlan: () => service().POST('/api/v1/pipeline/plans', {}).then(data),
  pipelineGetPlan: (plan_id: string, signal?: AbortSignal) => service().GET('/api/v1/pipeline/plans/{plan_id}', { params: { path: { plan_id } }, signal }).then(data),
  pipelineAdmit: (body: OperationRequest) => service().POST('/api/v1/pipeline/operations', { body }).then(data),
  pipelineOperations: (signal?: AbortSignal) => service().GET('/api/v1/pipeline/operations', { signal }).then(data),
  pipelineOperation: (operation_id: string, signal?: AbortSignal) => service().GET('/api/v1/pipeline/operations/{operation_id}', { params: { path: { operation_id } }, signal }).then(data),
  pipelineApproveSecond: (operation_id: string, body: OperationRequest) => service().POST('/api/v1/pipeline/operations/{operation_id}/approve', { params: { path: { operation_id } }, body }).then(data),
  clusterBuildPlan: (body: ClusterBuildPlanRequest) => service().POST('/api/v1/cluster-build/plans', { body }).then(data),
  clusterBuildGetPlan: (plan_id: string, signal?: AbortSignal) => service().GET('/api/v1/cluster-build/plans/{plan_id}', { params: { path: { plan_id } }, signal }).then(data),
  clusterBuildAdmit: (body: ClusterBuildOperationRequest) => service().POST('/api/v1/cluster-build/operations', { body }).then(data),
  clusterBuildOperations: (signal?: AbortSignal) => service().GET('/api/v1/cluster-build/operations', { signal }).then(data),
  clusterBuildOperation: (operation_id: string, signal?: AbortSignal) => service().GET('/api/v1/cluster-build/operations/{operation_id}', { params: { path: { operation_id } }, signal }).then(data),
  composition: (signal?: AbortSignal) => service().GET('/api/v1/composition', { signal }).then(data),
  compositionEnvironment: (env: string, signal?: AbortSignal) => service().GET('/api/v1/composition/environments/{env}', { params: { path: { env } }, signal }).then(data),
  compositionHistory: (signal?: AbortSignal) => service().GET('/api/v1/composition/history', { signal }).then(data),
  compositionEnvironmentHistory: (env: string, signal?: AbortSignal) => service().GET('/api/v1/composition/environments/{env}/history', { params: { path: { env } }, signal }).then(data),
  compositionSync: (body: CompositionSyncRequest) => service().POST('/api/v1/composition/sync', { body }).then(data),
  clusterStatus: (signal?: AbortSignal) => service().GET('/api/v1/cluster/status', { signal }).then(data),
  profiles: (signal?: AbortSignal) => service().GET('/api/v1/profiles', { signal }).then(data),
  switchProfile: (body: ProfileSwitchRequest) => service().POST('/api/v1/profiles/switch', { body }).then(data),
  namedEnvironmentActions: (env: string, signal?: AbortSignal) => service().GET('/api/v1/composition/environments/{env}/actions', { params: { path: { env } }, signal }).then(data),
  approveNamedEnvironment: (env: string, body: NamedEnvironmentApprovalRequest) => service().POST('/api/v1/composition/environments/{env}/approvals', { params: { path: { env } }, body }).then(data),
  promoteNamedEnvironment: (env: string, body: NamedEnvironmentPromotionRequest) => service().POST('/api/v1/composition/environments/{env}/promotions', { params: { path: { env } }, body }).then(data),
  wakeNamedEnvironment: (env: string) => service().POST('/api/v1/composition/environments/{env}/wake', { params: { path: { env } } }).then(data),
  workspaceLogSources: (scope: string[], signal?: AbortSignal) => service().GET('/api/v1/logs/sources', { params: { query: { scope } }, signal }).then(data),
  workspaceLogLines: (query: { stream: string[]; previous?: boolean; tail_lines?: number; since_seconds?: number | null; q?: string | null; level?: string[] }, signal?: AbortSignal) => service().GET('/api/v1/logs/lines', { params: { query }, signal }).then(data),
  forwards: (signal?: AbortSignal) => service().GET('/api/v1/forwards', { signal }).then(data),
  stopStaleForwards: () => service().POST('/api/v1/forwards/stale/stop', {}).then(data),
  navigation: (signal?: AbortSignal) => service().GET('/api/v1/navigation', { signal }).then(data),
  addProfileScope: (body: ProfileScopeRequest) => service().POST('/api/v1/profiles/scopes', { body }).then(data),
  removeProfileScope: (scope_id: string) => service().DELETE('/api/v1/profiles/scopes/{scope_id}', { params: { path: { scope_id } } }).then(() => undefined),

};
export const applicationPath = (id: string) => `/applications/${encodeURIComponent(id)}`;
type LogLink = { scope?: string | null; workload?: string | null; pod?: string | null; container?: string | null; previous?: boolean; q?: string | null };
/** A URL-backed Logs workspace filter; every field is optional. */
export function logsPath(link: LogLink = {}): string {
  const params = new URLSearchParams();
  if (link.scope) params.append('scope', link.scope);
  for (const key of ['workload', 'pod', 'container', 'q'] as const) if (link[key]) params.set(key, link[key]!);
  if (link.previous) params.set('previous', '1');
  const query = params.toString();
  return query ? `/logs?${query}` : '/logs';
}
/** The Forwards workspace with a target preselected. */
export function forwardsPath(link: { application?: string | null; resource?: string | null; port?: number | null } = {}): string {
  const params = new URLSearchParams();
  if (link.application) params.set('application', link.application);
  if (link.resource) params.set('resource', link.resource);
  if (link.port) params.set('port', String(link.port));
  const query = params.toString();
  return query ? `/forwards?${query}` : '/forwards';
}
export function eventUrl(applicationId: string, cursor: string): string {
  const url = new URL(`${basePath()}/api/v1/events`, window.location.origin);
  url.searchParams.set('application_id', applicationId);
  url.searchParams.set('after', cursor);
  return url.toString();
}
export function observationUrl(applicationId: string, cursor: string): string {
  const url = new URL(`${basePath()}/api/v1/observation-events`, window.location.origin);
  url.searchParams.set('application_id', applicationId);
  url.searchParams.set('after', cursor);
  return url.toString();
}
