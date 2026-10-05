import { useCallback, useEffect, useState } from 'react';
import type {
  MediaServerSettings,
  MediaServerSettingsInput,
  MediaServerTestResult,
} from '@/types/mediaServer';
import { errorMessage } from '@/services/apiClient';
import { getMediaServerSettings, saveMediaServerSettings, testMediaServerSettings } from '@/services/mediaServerService';

export interface UseMediaServerSettingsReturn {
  settings: MediaServerSettings | null;
  isLoading: boolean;
  isSaving: boolean;
  isTesting: boolean;
  /** Load failure, or null. */
  error: string | null;
  save: (input: MediaServerSettingsInput) => Promise<{ ok: boolean; message: string }>;
  test: (input: MediaServerSettingsInput) => Promise<MediaServerTestResult>;
}

/** Loads and saves the media server chosen on the Settings page. Secrets stay masked (`********`). */
export function useMediaServerSettings(enabled: boolean): UseMediaServerSettingsReturn {
  const [settings, setSettings] = useState<MediaServerSettings | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(enabled);
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [isTesting, setIsTesting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setIsLoading(true);
    getMediaServerSettings()
      .then((next) => {
        if (cancelled) return;
        setSettings(next);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorMessage(err, 'Could not load media server settings'));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  const save = useCallback(async (input: MediaServerSettingsInput) => {
    setIsSaving(true);
    try {
      setSettings(await saveMediaServerSettings(input));
      return { ok: true, message: 'Media server settings saved' };
    } catch (err: unknown) {
      return { ok: false, message: errorMessage(err, 'Failed to save media server settings') };
    } finally {
      setIsSaving(false);
    }
  }, []);

  const test = useCallback(async (input: MediaServerSettingsInput): Promise<MediaServerTestResult> => {
    setIsTesting(true);
    try {
      return await testMediaServerSettings(input);
    } catch (err: unknown) {
      return { ok: false, message: errorMessage(err, 'Could not run the connection test') };
    } finally {
      setIsTesting(false);
    }
  }, []);

  return { settings, isLoading, isSaving, isTesting, error, save, test };
}
