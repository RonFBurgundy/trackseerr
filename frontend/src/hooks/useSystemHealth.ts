import { useState, useEffect, useCallback } from 'react';
import type { Schema } from '@/types/apiSchema';
import {
  getSystemHealth,
  getHealthSettings,
  updateHealthSettings,
  triggerScheduledTask,
} from '@/services/systemService';
import { errorMessage } from '@/services/apiClient';

export type SystemHealthResponse = Schema<'SystemHealthResponse'>;
export type HealthCheckItem = Schema<'HealthCheckItem'>;
export type HealthSettings = Schema<'HealthSettings'>;

export interface UseSystemHealthReturn {
  health: SystemHealthResponse | null;
  settings: HealthSettings | null;
  loading: boolean;
  checking: boolean;
  saving: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  runNow: () => Promise<void>;
  saveSettings: (next: HealthSettings) => Promise<void>;
}

export function useSystemHealth(enabled: boolean = true): UseSystemHealthReturn {
  const [health, setHealth] = useState<SystemHealthResponse | null>(null);
  const [settings, setSettings] = useState<HealthSettings | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [checking, setChecking] = useState<boolean>(false);
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    try {
      const [h, s] = await Promise.all([getSystemHealth(), getHealthSettings()]);
      setHealth(h);
      setSettings(s);
      setError(null);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to load system health'));
    }
  }, [enabled]);

  useEffect(() => {
    if (!enabled) {
      setHealth(null);
      setSettings(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    Promise.all([getSystemHealth(), getHealthSettings()])
      .then(([h, s]) => {
        if (!cancelled) {
          setHealth(h);
          setSettings(s);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(errorMessage(err, 'Failed to load system health'));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [enabled]);

  const runNow = useCallback(async (): Promise<void> => {
    setChecking(true);
    try {
      await triggerScheduledTask('health_check');
      await new Promise((resolve) => setTimeout(resolve, 3000));
      await refresh();
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to run health check'));
    } finally {
      setChecking(false);
    }
  }, [refresh]);

  const saveSettings = useCallback(
    async (next: HealthSettings): Promise<void> => {
      setSaving(true);
      try {
        const updated = await updateHealthSettings(next);
        setSettings(updated);
        setError(null);
      } catch (err: unknown) {
        setError(errorMessage(err, 'Failed to save health settings'));
        throw err;
      } finally {
        setSaving(false);
      }
    },
    []
  );

  return {
    health,
    settings,
    loading,
    checking,
    saving,
    error,
    refresh,
    runNow,
    saveSettings,
  };
}
