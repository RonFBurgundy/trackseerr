import { useEffect, useState } from 'react';
import type { GatewayStatus } from '@/types/deployment';
import { getGatewayStatus } from '@/services/deploymentService';
import { errorMessage } from '@/services/apiClient';

export const GATEWAY_STATUS_REFRESH_MS = 30_000;

export interface UseGatewayStatusReturn {
  status: GatewayStatus | null;
  error: string | null;
}

/** Polls the request-portal status every 30s while `enabled` (card visible); stops on unmount. */
export function useGatewayStatus(enabled: boolean): UseGatewayStatusReturn {
  const [status, setStatus] = useState<GatewayStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const next = await getGatewayStatus();
        if (!cancelled) {
          setStatus(next);
          setError(null);
        }
      } catch (err: unknown) {
        if (!cancelled) setError(errorMessage(err, 'Failed to load request portal status'));
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), GATEWAY_STATUS_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [enabled]);

  return { status, error };
}
