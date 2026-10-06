import { useCallback, useRef, useState } from 'react';
import type { DiscoveryItem, DiscoveryTrackDetail } from '@/types/models';
import { getDiscoveryTrackDetail } from '@/services/discoveryService';

export interface UseDiscoveryTrackReturn {
  /** The item the modal was opened with; shown immediately while `detail` loads. */
  track: DiscoveryItem | null;
  detail: DiscoveryTrackDetail | null;
  isLoading: boolean;
  error: string | null;
  open: (item: DiscoveryItem) => Promise<void>;
  close: () => void;
}

/** Track detail modal state: the opened track plus its lazily loaded metadata. */
export function useDiscoveryTrack(): UseDiscoveryTrackReturn {
  const [track, setTrack] = useState<DiscoveryItem | null>(null);
  const [detail, setDetail] = useState<DiscoveryTrackDetail | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef<number>(0);

  const open = useCallback(async (item: DiscoveryItem): Promise<void> => {
    const ticket = ++generation.current;
    setTrack(item);
    setDetail(null);
    setError(null);
    setIsLoading(true);
    try {
      const data = await getDiscoveryTrackDetail(item.id);
      if (ticket === generation.current) setDetail(data);
    } catch (err: unknown) {
      if (ticket === generation.current) {
        setError(err instanceof Error ? err.message : 'Failed to load track details');
      }
    } finally {
      if (ticket === generation.current) setIsLoading(false);
    }
  }, []);

  const close = useCallback((): void => {
    generation.current += 1;
    setTrack(null);
    setDetail(null);
    setError(null);
    setIsLoading(false);
  }, []);

  return { track, detail, isLoading, error, open, close };
}
