import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from '@/services/apiClient';
import { dismissSlskdChallenge, getSlskdChallenges, replyToSlskdChallenge } from '@/services/slskdChallengeService';
import type { SlskdChallenge } from '@/types/slskdChallenges';
import { usePolling } from './usePolling';

const POLL_MS = 60_000;

export interface UseSlskdChallengesOptions {
  onToast: (message: string, tone?: 'ok' | 'error') => void;
}

export interface UseSlskdChallengesReturn {
  items: SlskdChallenge[];
  busyId: string | null;
  reply: (id: string, text: string) => Promise<void>;
  dismiss: (id: string) => Promise<void>;
  refresh: () => Promise<void>;
}

/** Loads open Soulseek sharer challenges, polls every 60 s, and sends replies or dismissals. */
export function useSlskdChallenges({ onToast }: UseSlskdChallengesOptions): UseSlskdChallengesReturn {
  const [items, setItems] = useState<SlskdChallenge[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const onToastRef = useRef(onToast);
  onToastRef.current = onToast;

  const refresh = useCallback(async (): Promise<void> => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      const list = await getSlskdChallenges(controller.signal);
      if (controller.signal.aborted) return;
      setItems(list.items);
    } catch (err: unknown) {
      if (controller.signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) return;
      console.warn('Slskd challenges failed', err);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return () => controllerRef.current?.abort();
  }, [refresh]);

  usePolling(() => void refresh(), POLL_MS);

  const reply = useCallback(
    async (id: string, text: string): Promise<void> => {
      setBusyId(id);
      try {
        const res = await replyToSlskdChallenge(id, text);
        onToastRef.current(`Reply sent to ${res.username}`);
      } catch (err: unknown) {
        onToastRef.current(errorMessage(err, 'Could not send the reply'), 'error');
      } finally {
        setBusyId(null);
      }
      await refresh();
    },
    [refresh]
  );

  const dismiss = useCallback(
    async (id: string): Promise<void> => {
      setBusyId(id);
      try {
        await dismissSlskdChallenge(id);
      } catch (err: unknown) {
        onToastRef.current(errorMessage(err, 'Could not dismiss the message'), 'error');
      } finally {
        setBusyId(null);
      }
      await refresh();
    },
    [refresh]
  );

  return { items, busyId, reply, dismiss, refresh };
}
