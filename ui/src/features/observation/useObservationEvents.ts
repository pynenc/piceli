import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { observationUrl } from '../../api/client';

export function useObservationEvents(applicationId: string, cursor?: string | null) {
  const client = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [gap, setGap] = useState(false);
  useEffect(() => {
    if (cursor == null) return;
    const source = new EventSource(observationUrl(applicationId, cursor));
    const refresh = () => {
      void client.invalidateQueries({ queryKey: ['resources', applicationId] });
      void client.invalidateQueries({ queryKey: ['resource', applicationId] });
      void client.invalidateQueries({ queryKey: ['log-sources', applicationId] });
      void client.invalidateQueries({ queryKey: ['application', applicationId] });
    };
    const reset = () => { setGap(true); refresh(); };
    source.addEventListener('upsert', refresh);
    source.addEventListener('delete', refresh);
    source.addEventListener('reset', reset);
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    return () => { source.close(); setConnected(false); };
  }, [applicationId, cursor, client]);
  return { connected, gap };
}
