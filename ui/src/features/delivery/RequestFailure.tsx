import { ApiError } from '../../api/client';
import { Notice } from '../../components/State';
export function RequestFailure({ error }: { error: Error }) {
  const stale = error instanceof ApiError && ['plan-stale', 'plan-expired', 'preview-stale', 'preview-expired'].some(code => error.detail.code.includes(code));
  const preRollout = error instanceof ApiError && error.detail.code === 'ui-prerollout-unsupported';
  if (preRollout) return <Notice title="Pre-rollout checks need the Piceli CLI" danger><p>This application declares pre-rollout checks. Run <code>piceli deploy</code> from an authorized runner so those checks complete before any release is applied.</p><p className="small">{error.detail.code}{error.detail.correlation_id ? ` · Reference ${error.detail.correlation_id}` : ''}</p></Notice>;
  return <Notice title={stale ? 'Review a new plan' : 'Request not completed'} danger><p>{error instanceof ApiError ? error.message : 'The connection ended before a response was received. Check Activity for an admitted run, or retry this same request.'}</p>{stale && <p>The previous approval will not apply to a different plan.</p>}{error instanceof ApiError && <p className="small">{error.detail.code}{error.detail.correlation_id ? ` · Reference ${error.detail.correlation_id}` : ''}</p>}</Notice>;
}
