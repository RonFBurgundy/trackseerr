import { useEffect, useState } from 'react';

/** Wall-clock `Date.now()` re-read every `intervalMs` while `enabled`, for live countdowns and elapsed timers. */
export function useNow(intervalMs: number = 1000, enabled: boolean = true): number {
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    if (!enabled) return undefined;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, enabled]);
  return now;
}
