import { useCallback, useEffect, useState } from 'react';
import type { ListeningPlaylistPayload, ListeningSources } from '@/types/listening';
import { createListeningPlaylist, getListeningSources } from '@/services/listeningService';
import { errorMessage } from '@/services/apiClient';

export interface UseListeningSourcesReturn {
  sources: ListeningSources | null;
  isLoading: boolean;
  isCreating: boolean;
  error: string | null;
  /** Resolves to an error message, or null once the playlist was created. */
  create: (payload: ListeningPlaylistPayload) => Promise<string | null>;
}

/** Loads the user's linked listening accounts and lists while `enabled` (the modal is open) and creates playlists from them. */
export function useListeningSources(enabled: boolean): UseListeningSourcesReturn {
  const [sources, setSources] = useState<ListeningSources | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [isCreating, setIsCreating] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    getListeningSources()
      .then((data) => {
        if (!cancelled) setSources(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorMessage(err, 'Failed to load your listening accounts'));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  const create = useCallback(async (payload: ListeningPlaylistPayload): Promise<string | null> => {
    setIsCreating(true);
    try {
      await createListeningPlaylist(payload);
      return null;
    } catch (err: unknown) {
      return errorMessage(err, 'Failed to create the playlist');
    } finally {
      setIsCreating(false);
    }
  }, []);

  return { sources, isLoading, isCreating, error, create };
}
