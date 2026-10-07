import { useCallback, useState } from 'react';
import type { ArtistDiscographyAlbum, DiscoveryItem } from '@/types/models';
import { errorMessage } from '@/services/apiClient';

export interface UseProfileRequestsOptions {
  onRequest: (item: DiscoveryItem) => Promise<void>;
  onRequestDiscography: (artist: string, albums: ArtistDiscographyAlbum[]) => Promise<void>;
  /** Called after a successful request so the profile picks up the new statuses. */
  onDone: () => Promise<void>;
}

export interface UseProfileRequestsReturn {
  /** Ids with a request in flight. */
  busyIds: ReadonlySet<string>;
  /** True while a bulk discography request is in flight. */
  bulkBusy: boolean;
  error: string | null;
  clearError: () => void;
  requestItem: (item: DiscoveryItem) => Promise<void>;
  requestMany: (artist: string, albums: ArtistDiscographyAlbum[]) => Promise<void>;
}

/** Request actions for the artist profile and the library "rest of discography" (releases the metadata profile filters out): progress and server errors (quota, duplicate). */
export function useProfileRequests({ onRequest, onRequestDiscography, onDone }: UseProfileRequestsOptions): UseProfileRequestsReturn {
  const [busyIds, setBusyIds] = useState<ReadonlySet<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const requestItem = useCallback(
    async (item: DiscoveryItem): Promise<void> => {
      setError(null);
      setBusyIds((prev) => new Set(prev).add(item.id));
      try {
        await onRequest(item);
        await onDone();
      } catch (err: unknown) {
        setError(errorMessage(err, 'Request failed'));
      } finally {
        setBusyIds((prev) => {
          const next = new Set(prev);
          next.delete(item.id);
          return next;
        });
      }
    },
    [onRequest, onDone]
  );

  const requestMany = useCallback(
    async (artist: string, albums: ArtistDiscographyAlbum[]): Promise<void> => {
      setError(null);
      setBulkBusy(true);
      try {
        await onRequestDiscography(artist, albums);
        await onDone();
      } catch (err: unknown) {
        setError(errorMessage(err, 'Failed to request discography'));
      } finally {
        setBulkBusy(false);
      }
    },
    [onRequestDiscography, onDone]
  );

  const clearError = useCallback(() => setError(null), []);

  return { busyIds, bulkBusy, error, clearError, requestItem, requestMany };
}
