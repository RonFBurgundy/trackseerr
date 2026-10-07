import { useCallback, useEffect, useState } from 'react';
import type { LogSettings, LogSettingsUpdate } from '@/types/models';
import { getLogSettings, updateLogSettings } from '@/services/systemService';
import { errorMessage } from '@/services/apiClient';

export interface UseLogSettingsReturn {
  settings: LogSettings | null;
  isLoading: boolean;
  isSaving: boolean;
  error: string | null;
  /** Resolves null when the server accepted and applied the change, else the error message. */
  save: (update: LogSettingsUpdate) => Promise<string | null>;
}

/** Admin log settings (rotation, retention, size cap, level); loads while `enabled` is true. */
export function useLogSettings(enabled: boolean): UseLogSettingsReturn {
  const [settings, setSettings] = useState<LogSettings | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return undefined;
    let active = true;
    setIsLoading(true);
    setError(null);
    getLogSettings()
      .then((res) => {
        if (active) setSettings(res);
      })
      .catch((err: unknown) => {
        if (active) setError(errorMessage(err, 'Failed to load log settings'));
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [enabled]);

  const save = useCallback(async (update: LogSettingsUpdate): Promise<string | null> => {
    setIsSaving(true);
    setError(null);
    try {
      setSettings(await updateLogSettings(update));
      return null;
    } catch (err: unknown) {
      const message = errorMessage(err, 'Failed to save log settings');
      setError(message);
      return message;
    } finally {
      setIsSaving(false);
    }
  }, []);

  return { settings, isLoading, isSaving, error, save };
}
