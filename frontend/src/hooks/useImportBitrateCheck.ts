import { useCallback, useEffect, useState } from 'react';
import type { ImportBitrateCheck } from '@/types/models';
import { errorMessage } from '@/services/apiClient';
import { getMediaManagementSettings, updateMediaManagementSettings } from '@/services/settingsService';

export interface UseImportBitrateCheckReturn {
  mode: ImportBitrateCheck;
  loading: boolean;
  saving: boolean;
  setMode: (next: ImportBitrateCheck) => Promise<void>;
}

type Toast = (msg: string, tone?: 'ok' | 'error') => void;

const isMode = (v: unknown): v is ImportBitrateCheck => v === 'off' || v === 'warn' || v === 'reject';

/** The `import_bitrate_check` media-management setting: loaded while `enabled`, saved on change. */
export function useImportBitrateCheck(enabled: boolean, onToast: Toast): UseImportBitrateCheckReturn {
  const [mode, setModeState] = useState<ImportBitrateCheck>('warn');
  const [loading, setLoading] = useState<boolean>(false);
  const [saving, setSaving] = useState<boolean>(false);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setLoading(true);
    getMediaManagementSettings()
      .then((s) => {
        if (!cancelled && isMode(s.import_bitrate_check)) setModeState(s.import_bitrate_check);
      })
      .catch((err: unknown) => {
        if (!cancelled) onToast(errorMessage(err, 'Failed to load import bitrate check'), 'error');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, onToast]);

  const setMode = useCallback(
    async (next: ImportBitrateCheck): Promise<void> => {
      const previous = mode;
      setModeState(next);
      setSaving(true);
      try {
        const saved = await updateMediaManagementSettings({ import_bitrate_check: next });
        if (isMode(saved.import_bitrate_check)) setModeState(saved.import_bitrate_check);
      } catch (err: unknown) {
        setModeState(previous);
        onToast(errorMessage(err, 'Failed to save import bitrate check'), 'error');
      } finally {
        setSaving(false);
      }
    },
    [mode, onToast]
  );

  return { mode, loading, saving, setMode };
}
