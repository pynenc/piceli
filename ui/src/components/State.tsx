import type { ReactNode } from 'react';
import { ApiError } from '../api/client';
import type { Freshness } from '../api/generated';

const names: Record<string, string> = { in_sync: 'In sync', changes: 'Changes available', external: 'Externally managed', unknown: 'Unknown', idle: 'Idle' };
export function label(value: string) { return names[value] ?? value.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase()); }
export function Badge({ value }: { value: string }) {
  const tone = ['healthy', 'in_sync', 'connected', 'succeeded', 'synced', 'deployed', 'unchanged'].includes(value) ? 'good' : ['degraded', 'failed', 'missing', 'unavailable', 'down'].includes(value) ? 'bad' : ['stale', 'reconnecting', 'progressing', 'changes', 'drifted', 'running', 'building', 'rolling', 'pending', 'retrying', 'approval-required'].includes(value) ? 'warn' : 'neutral';
  return <span className={`badge ${tone}`}><span aria-hidden="true">●</span>{label(value)}</span>;
}
export function Notice({ title, children, danger = false }: { title: string; children?: ReactNode; danger?: boolean }) {
  return <div className={`notice ${danger ? 'danger' : ''}`} role="status"><strong>{title}</strong>{children && <div>{children}</div>}</div>;
}
export function Loading({ text = 'Loading observed state…' }: { text?: string }) { return <p className="loading" role="status">{text}</p>; }
export function Failure({ error, retry }: { error: Error; retry?: () => void }) {
  const denied = error instanceof ApiError && [401, 403].includes(error.status);
  return <Notice title={denied ? 'Permission required' : 'Observation unavailable'} danger>
    <p>{error instanceof ApiError ? error.message : 'Cannot reach the Piceli service. Check your connection and try again.'}</p>
    {error instanceof ApiError && error.detail.correlation_id && <p className="small">Reference: {error.detail.correlation_id}</p>}
    {retry && <button onClick={retry}>Try again</button>}
  </Notice>;
}
export function FreshnessNotice({ freshness, disconnected = false }: { freshness: Freshness; disconnected?: boolean }) {
  if (freshness.state === 'connected' && !disconnected) return null;
  return <Notice title={disconnected ? 'Connection lost · showing last observed data' : `${label(freshness.state)} observation`}>
    <p>Health and desired/live state may be out of date.{freshness.reason ? ` ${freshness.reason}` : ''}</p>
    {freshness.observed_at && <p className="small">Last observed {formatTime(freshness.observed_at)}</p>}
  </Notice>;
}
export function formatTime(value?: string | null) {
  if (!value) return 'Not yet observed';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? 'Observation time unavailable' : date.toLocaleString();
}
