import { useEffect, useRef } from 'react';

/**
 * Calls `callback` every `intervalMs` while the document is visible. The timer is paused when the tab
 * is hidden and fires once immediately when it becomes visible again.
 */
export function usePolling(callback: () => void, intervalMs: number, enabled: boolean = true): void {
  const cbRef = useRef<() => void>(callback);
  cbRef.current = callback;

  useEffect(() => {
    if (!enabled) return undefined;
    let timer: number | null = null;

    const stop = (): void => {
      if (timer !== null) {
        window.clearInterval(timer);
        timer = null;
      }
    };
    const start = (): void => {
      stop();
      timer = window.setInterval(() => cbRef.current(), intervalMs);
    };
    const onVisibility = (): void => {
      if (document.visibilityState === 'visible') {
        cbRef.current();
        start();
      } else {
        stop();
      }
    };

    if (document.visibilityState === 'visible') start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [intervalMs, enabled]);
}
