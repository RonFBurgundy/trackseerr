import { useCallback, useEffect, useRef, useState } from 'react';
import type { ArtistProfile, ArtistProfileTarget } from '@/types/models';
import { ApiError, errorMessage } from '@/services/apiClient';
import { getArtistProfile } from '@/services/discoveryService';

export interface UseArtistProfileReturn {
  profile: ArtistProfile | null;
  isLoading: boolean;
  error: string | null;
  /** The server answered 404: the artist id is unknown (stale link). */
  notFound: boolean;
  /** Reload in the background: the current profile stays on screen (used after a request changes statuses). */
  refetch: () => Promise<void>;
}

/** Stable key so a fresh `{ discoveryId }` object each render does not retrigger the load. */
function targetKey(target: ArtistProfileTarget | null): string | null {
  if (target === null) return null;
  return 'discoveryId' in target ? `d:${target.discoveryId}` : `l:${target.libraryArtistId}`;
}

/** Loads one artist profile; `null` target means "do not load" (non-admin, or not yet needed). */
export function useArtistProfile(target: ArtistProfileTarget | null): UseArtistProfileReturn {
  const key = targetKey(target);
  const targetRef = useRef<ArtistProfileTarget | null>(target);
  targetRef.current = target;
  const generation = useRef<number>(0);
  const [profile, setProfile] = useState<ArtistProfile | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(key !== null);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState<boolean>(false);

  const load = useCallback(async (background: boolean): Promise<void> => {
    const current = targetRef.current;
    const ticket = ++generation.current;
    if (current === null) return;
    if (!background) {
      setIsLoading(true);
      setProfile(null);
    }
    setError(null);
    setNotFound(false);
    try {
      const data = await getArtistProfile(current);
      if (ticket === generation.current) setProfile(data);
    } catch (err: unknown) {
      if (ticket !== generation.current) return;
      if (err instanceof ApiError && err.status === 404) setNotFound(true);
      setError(errorMessage(err, 'Failed to load artist profile'));
    } finally {
      if (ticket === generation.current) setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (key === null) {
      generation.current += 1;
      setProfile(null);
      setIsLoading(false);
      setError(null);
      setNotFound(false);
      return;
    }
    void load(false);
    return () => {
      generation.current += 1;
    };
  }, [key, load]);

  const refetch = useCallback((): Promise<void> => load(true), [load]);

  return { profile, isLoading, error, notFound, refetch };
}
