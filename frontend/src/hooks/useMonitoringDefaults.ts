import { useEffect, useState } from 'react';
import type { MonitorOption } from '@/types/monitoring';
import { getMediaManagementSettings } from '@/services/settingsService';

export interface UseMonitoringDefaultsReturn {
  /** Monitor option to pre-select when adding an artist manually. */
  addMonitorOption: MonitorOption;
  /** Option applied to artists found by a library scan. */
  scanMonitorOption: MonitorOption;
  loaded: boolean;
}

/** Server-configured monitoring defaults (media-management settings), falling back to the documented defaults. */
export function useMonitoringDefaults(): UseMonitoringDefaultsReturn {
  const [state, setState] = useState<UseMonitoringDefaultsReturn>({
    addMonitorOption: 'existing',
    scanMonitorOption: 'existing',
    loaded: false,
  });

  useEffect(() => {
    let cancelled = false;
    getMediaManagementSettings()
      .then((s) => {
        if (cancelled) return;
        setState({
          addMonitorOption: s.add_monitor_option ?? 'existing',
          scanMonitorOption: s.scan_monitor_option ?? 'existing',
          loaded: true,
        });
      })
      .catch(() => {
        // Non-admins cannot read settings; the documented defaults stay in effect.
        if (!cancelled) setState((prev) => ({ ...prev, loaded: true }));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return state;
}
