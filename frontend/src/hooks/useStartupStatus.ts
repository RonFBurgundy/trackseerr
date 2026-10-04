import { useEffect, useState } from 'react';
import { fetchHealth } from '@/services/healthService';

export const STARTUP_POLL_MS = 2000;

export type StartupPhase = 'checking' | 'starting' | 'ready';

export interface UseStartupStatusReturn {
  phase: StartupPhase;
  step: string | null;
}

/**
 * Polls /api/health until the server stops reporting "starting". An unreachable health endpoint on
 * the very first check counts as ready so the app can surface its own connection errors instead of
 * hanging on a loading screen.
 */
export function useStartupStatus(): UseStartupStatusReturn {
  const [phase, setPhase] = useState<StartupPhase>('checking');
  const [step, setStep] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    let sawStarting = false;

    const tick = async () => {
      let health = null;
      try {
        health = await fetchHealth();
      } catch (err: unknown) {
        console.error('Startup health check failed', err);
      }
      if (cancelled) return;
      if (health && health.status === 'starting') {
        sawStarting = true;
        setPhase('starting');
        setStep(health.step ?? null);
        timer = window.setTimeout(() => void tick(), STARTUP_POLL_MS);
      } else if (health === null && sawStarting) {
        // Server restarting mid-boot: keep waiting rather than dropping into a broken app.
        timer = window.setTimeout(() => void tick(), STARTUP_POLL_MS);
      } else {
        setPhase('ready');
        setStep(null);
      }
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  return { phase, step };
}
