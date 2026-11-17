import { useCallback, useEffect, useRef, useState } from 'react';
import type { MatchApplyResponse, MatchCandidate, MatchSuggestionsResponse } from '@/types/models';
import { applyMatch, getMatchSuggestions } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';

export interface UseMatchSuggestionsReturn {
  data: MatchSuggestionsResponse | null;
  isLoading: boolean;
  error: string | null;
  isApplying: boolean;
  applyError: string | null;
  /** Applies one candidate to the album. Rejects with the original error after setting `applyError`. */
  apply: (candidate: MatchCandidate) => Promise<MatchApplyResponse>;
  reload: () => void;
}

/** Loads MusicBrainz match suggestions for one unlinked album; `albumId` null means idle. */
export function useMatchSuggestions(albumId: string | null): UseMatchSuggestionsReturn {
  const [data, setData] = useState<MatchSuggestionsResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [isApplying, setIsApplying] = useState<boolean>(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState<number>(0);
  const albumRef = useRef<string | null>(albumId);
  albumRef.current = albumId;

  useEffect(() => {
    setData(null);
    setError(null);
    setApplyError(null);
    if (albumId === null) {
      setIsLoading(false);
      return;
    }
    const controller = new AbortController();
    setIsLoading(true);
    getMatchSuggestions(albumId, controller.signal)
      .then((res) => {
        if (controller.signal.aborted) return;
        setData(res);
        setIsLoading(false);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(errorMessage(err, 'Could not load matches'));
        setIsLoading(false);
      });
    return () => controller.abort();
  }, [albumId, reloadTick]);

  const apply = useCallback(async (candidate: MatchCandidate): Promise<MatchApplyResponse> => {
    const id = albumRef.current;
    if (id === null) throw new Error('No album selected');
    setIsApplying(true);
    setApplyError(null);
    try {
      const track_recordings: Record<string, string> = {};
      for (const m of candidate.track_map) {
        if (m.recording_id) track_recordings[m.track_id] = m.recording_id;
      }
      return await applyMatch(id, {
        release_group: {
          id: candidate.id,
          title: candidate.title,
          artist_credit: candidate.artist_credit,
          first_release_date: candidate.first_release_date,
          is_various: candidate.is_various,
          primary_type: candidate.primary_type,
          secondary_types: candidate.secondary_types,
        },
        track_recordings,
      });
    } catch (err: unknown) {
      setApplyError(errorMessage(err, 'Could not apply this match'));
      throw err;
    } finally {
      setIsApplying(false);
    }
  }, []);

  const reload = useCallback(() => setReloadTick((n) => n + 1), []);

  return { data, isLoading, error, isApplying, applyError, apply, reload };
}
