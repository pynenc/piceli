import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { eventUrl } from '../../api/client';
import type { Event as ServiceEvent } from '../../api/generated';

/** Resume at the cursor captured before the operation snapshot was read. */
export function useDeliveryEvents(applicationId: string, cursor?: string): { connected: boolean; gap: boolean } {
  const client = useQueryClient();
  const [start, setStart] = useState<{ applicationId: string; cursor: string } | null>(null);
  const [connected, setConnected] = useState(false);
  const [gap, setGap] = useState(false);
  useEffect(() => {
    if (cursor && start?.applicationId !== applicationId) setStart({ applicationId, cursor });
  }, [applicationId, cursor, start]);
  useEffect(() => {
    if (!start || start.applicationId !== applicationId || typeof EventSource === 'undefined') return;
    const source = new EventSource(eventUrl(applicationId, start.cursor));
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    const refresh = (frame: globalThis.Event) => {
      try {
        const event = JSON.parse((frame as MessageEvent).data) as ServiceEvent;
        if (event.scope !== applicationId) return;
        void client.invalidateQueries({ queryKey: ['operations', applicationId] });
        void client.invalidateQueries({ queryKey: ['operation'] });
        void client.invalidateQueries({ queryKey: ['application', applicationId] });
        void client.invalidateQueries({ queryKey: ['releases', applicationId] });
        if (event.kind === 'reset') {
          setGap(true);
          void client.invalidateQueries({ queryKey: ['resources', applicationId] });
          void client.invalidateQueries({ queryKey: ['applications'] });
        }
      } catch { /* A malformed frame cannot replace the existing snapshot. */ }
    };
    for (const kind of ['upsert', 'operation', 'reset']) source.addEventListener(kind, refresh);
    return () => { source.close(); setConnected(false); };
  }, [applicationId, client, start]);
  return { connected, gap };
}
