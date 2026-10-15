import { useEffect, useRef } from 'react';
import type { AppRoute } from './useAppRoute';
import { useToast, type UseToastReturn } from './useToast';
import { linkMbRelease } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';

export interface UseMbReturnOptions {
  enabled?: boolean;
  onNavigate: (route: AppRoute) => void;
  onToast?: (message: string, tone?: 'ok' | 'error') => void;
}

export function useMbReturn({ enabled = true, onNavigate, onToast }: UseMbReturnOptions): UseToastReturn {
  const { toast, showToast } = useToast();
  const handledRef = useRef<boolean>(false);

  useEffect(() => {
    if (!enabled || handledRef.current) return;

    const params = new URLSearchParams(window.location.search);
    const mbAlbum = params.get('mb_album');
    const releaseMbid = params.get('release_mbid');

    if (!mbAlbum || !releaseMbid) return;

    handledRef.current = true;

    // Strip both parameters with history.replaceState, keeping the hash and any remaining search params
    params.delete('mb_album');
    params.delete('release_mbid');
    const remaining = params.toString();
    const newSearch = remaining ? `?${remaining}` : '';
    const newUrl = `${window.location.pathname}${newSearch}${window.location.hash}`;
    window.history.replaceState(window.history.state, '', newUrl);

    linkMbRelease(mbAlbum, releaseMbid)
      .then(() => {
        const msg = 'Linked to MusicBrainz release';
        showToast(msg, 'ok');
        onToast?.(msg, 'ok');
        onNavigate({ tab: 'library', sub: 'albums', detail: { albumId: mbAlbum } });
      })
      .catch((err: unknown) => {
        const msg = errorMessage(err, 'Failed to link MusicBrainz release');
        showToast(msg, 'error');
        onToast?.(msg, 'error');
        onNavigate({ tab: 'library', sub: 'albums', detail: { albumId: mbAlbum } });
      });
  }, [enabled, onNavigate, onToast, showToast]);

  return { toast, showToast };
}
