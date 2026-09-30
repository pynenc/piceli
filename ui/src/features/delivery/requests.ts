/** Persist non-secret request identities across refresh, not credentials or approvals. */
export function requestKey(action: string, identity: string): string {
  const name = `piceli.request:${action}:${identity}`;
  try {
    const existing = sessionStorage.getItem(name);
    if (existing) return existing;
    const key = crypto.randomUUID();
    sessionStorage.setItem(name, key);
    return key;
  } catch {
    // A tab-local fallback keeps retries idempotent when storage is disabled.
    const existing = fallback.get(name);
    if (existing) return existing;
    const key = crypto.randomUUID();
    fallback.set(name, key);
    return key;
  }
}
const fallback = new Map<string, string>();
