import { useEffect, useState } from 'react';

/** Keep approval expiry current while its review remains open. Invalid dates fail closed. */
export function useExpired(expiresAt: string) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  return !Number.isFinite(Date.parse(expiresAt)) || now >= Date.parse(expiresAt);
}
