import { useCallback, useState } from 'react';
import { ApiError, errorMessage } from '@/services/apiClient';
import { createIssue } from '@/services/issueService';
import { search } from '@/services/discoveryService';
import { suggestTrackMatch } from '@/services/trackFixService';
import type { DiscoveryItem } from '@/types/models';

export interface FindTrackSource {
  title: string;
  artist: string;
}

export interface UseFindTrackReturn {
  results: DiscoveryItem[];
  searching: boolean;
  searched: boolean;
  submitting: boolean;
  error: string | null;
  notice: string | null;
  runSearch: (query: string) => Promise<void>;
  /** Suggests `item` as the right song; resolves true when the server accepted it. */
  suggest: (item: DiscoveryItem, note: string) => Promise<boolean>;
  /** Reports the track to the admins; resolves true when the issue was filed. */
  report: (note: string) => Promise<boolean>;
}

function duplicateMessage(err: unknown, duplicate: string, fallback: string): string {
  if (err instanceof ApiError && err.status === 409) {
    return err.message && /already/i.test(err.message) ? err.message : duplicate;
  }
  return errorMessage(err, fallback);
}

/** Catalog search plus the suggest / report calls behind the "Fix" panel of a missing playlist track. */
export function useFindTrack(playlistId: string, playlistName: string, source: FindTrackSource): UseFindTrackReturn {
  const [results, setResults] = useState<DiscoveryItem[]>([]);
  const [searching, setSearching] = useState<boolean>(false);
  const [searched, setSearched] = useState<boolean>(false);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const runSearch = useCallback(async (query: string): Promise<void> => {
    const q = query.trim();
    if (!q) return;
    setSearching(true);
    setError(null);
    setNotice(null);
    try {
      setResults(await search(q, 'track'));
      setSearched(true);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Search failed'));
    } finally {
      setSearching(false);
    }
  }, []);

  const suggest = useCallback(
    async (item: DiscoveryItem, note: string): Promise<boolean> => {
      setSubmitting(true);
      setError(null);
      setNotice(null);
      try {
        const trimmed = note.trim();
        const res = await suggestTrackMatch(playlistId, {
          source_title: source.title,
          source_artist: source.artist,
          discovery_id: item.id,
          target_title: item.title,
          target_artist: item.artist ?? '',
          target_album: item.album ?? null,
          note: trimmed || null,
        });
        setNotice(res.status === 'approved' ? 'Match saved. It applies on the next sync.' : 'Suggestion sent. An admin will review it.');
        return true;
      } catch (err: unknown) {
        setError(duplicateMessage(err, 'Already reported', 'Failed to send the suggestion'));
        return false;
      } finally {
        setSubmitting(false);
      }
    },
    [playlistId, source.title, source.artist]
  );

  const report = useCallback(
    async (note: string): Promise<boolean> => {
      setSubmitting(true);
      setError(null);
      setNotice(null);
      try {
        const trimmed = note.trim();
        await createIssue({
          issue_type: 'playlist_match',
          media_title: source.title,
          artist: source.artist,
          problem_details: `${trimmed ? `${trimmed}\n` : ''}Playlist: ${playlistName}`,
        });
        setNotice('Reported to the admins.');
        return true;
      } catch (err: unknown) {
        setError(duplicateMessage(err, 'Already reported', 'Failed to send the report'));
        return false;
      } finally {
        setSubmitting(false);
      }
    },
    [playlistName, source.title, source.artist]
  );

  return { results, searching, searched, submitting, error, notice, runSearch, suggest, report };
}
