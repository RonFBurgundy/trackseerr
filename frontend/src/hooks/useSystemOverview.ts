import { useState, useEffect, useCallback } from 'react';
import type { SystemStatusInfo, LidarrHealth } from '@/types/models';
import { getSystemStatus } from '@/services/settingsService';
import { getLidarrHealth } from '@/services/systemService';
import { errorMessage } from '@/services/apiClient';

export interface UseSystemOverviewReturn {
  status: SystemStatusInfo | null;
  statusError: string | null;
  lidarrHealth: LidarrHealth | null;
  healthError: string | null;
  isLoading: boolean;
  refresh: () => Promise<void>;
}

/** System status plus, when `includeLidarrHealth`, the Lidarr health report. */
export function useSystemOverview(includeLidarrHealth: boolean): UseSystemOverviewReturn {
  const [status, setStatus] = useState<SystemStatusInfo | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [lidarrHealth, setLidarrHealth] = useState<LidarrHealth | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    const [statusResult, healthResult] = await Promise.allSettled([
      getSystemStatus(),
      includeLidarrHealth ? getLidarrHealth() : Promise.resolve(null),
    ]);
    if (statusResult.status === 'fulfilled') {
      setStatus(statusResult.value);
      setStatusError(null);
    } else {
      setStatusError(errorMessage(statusResult.reason, 'Failed to load system status'));
    }
    if (healthResult.status === 'fulfilled') {
      setLidarrHealth(healthResult.value);
      setHealthError(null);
    } else {
      setHealthError(errorMessage(healthResult.reason, 'Failed to load Lidarr health'));
    }
    setIsLoading(false);
  }, [includeLidarrHealth]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { status, statusError, lidarrHealth, healthError, isLoading, refresh };
}
