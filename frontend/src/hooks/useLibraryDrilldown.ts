import { useCallback, useEffect, useRef, useState } from 'react';
import type { AlbumItem } from '@/types/models';
import { getAlbumDetail } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';
import type { AppRoute, LibraryDetail, LibraryRoute, NavigateOptions } from './useAppRoute';

export interface UseLibraryDrilldownReturn {
  artistId: string | null;
  collectionId: string | null;
  albumId: string | null;
  /** The album shown in the modal; null while a deep-linked album is still loading. */
  album: AlbumItem | null;
  openArtist: (artistId: number | string) => void;
  openCollection: (collectionId: string) => void;
  openAlbum: (album: AlbumItem) => void;
  /** Close the album modal (one history step when it was opened in-app). */
  closeAlbum: () => void;
  /** Leave an artist or collection page for the list it came from. */
  closeDetail: () => void;
}

/** The library route without its drill-down. */
function withoutDetail(route: LibraryRoute): LibraryRoute {
  return { tab: 'library', sub: route.sub };
}

function withDetail(route: LibraryRoute, detail: LibraryDetail): LibraryRoute {
  return { tab: 'library', sub: route.sub, detail };
}

/**
 * Library drill-down (artist, collection, album modal) derived from the URL, so every level is a history entry:
 * Back, the on-screen back keys and swipe/mouse-back all step out one level at a time.
 */
export function useLibraryDrilldown(
  route: LibraryRoute,
  navigate: (route: LibraryRoute, options?: NavigateOptions) => void,
  navigateUp: (parent: AppRoute) => void,
  onToast: (message: string, tone?: 'ok' | 'error') => void
): UseLibraryDrilldownReturn {
  const artistId = route.detail?.artistId ?? null;
  const collectionId = artistId === null ? (route.detail?.collectionId ?? null) : null;
  const albumId = route.detail?.albumId ?? null;

  // Albums opened in-app arrive complete; deep links fetch the one they name.
  const known = useRef<Map<string, AlbumItem>>(new Map());
  const [fetched, setFetched] = useState<AlbumItem | null>(null);
  const routeRef = useRef(route);
  routeRef.current = route;

  const closeAlbum = useCallback((): void => {
    const current = routeRef.current;
    const detail = current.detail;
    if (!detail?.albumId) return;
    const parent: LibraryDetail = {};
    if (detail.artistId) parent.artistId = detail.artistId;
    else if (detail.collectionId) parent.collectionId = detail.collectionId;
    navigateUp(parent.artistId || parent.collectionId ? withDetail(current, parent) : withoutDetail(current));
  }, [navigateUp]);

  useEffect(() => {
    if (albumId === null || known.current.has(albumId)) return undefined;
    let cancelled = false;
    getAlbumDetail(albumId)
      .then((detail) => {
        if (cancelled) return;
        known.current.set(albumId, detail);
        setFetched(detail);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        onToast(errorMessage(err, 'Album not found'), 'error');
        closeAlbum();
      });
    return () => {
      cancelled = true;
    };
  }, [albumId, onToast, closeAlbum]);

  const album = albumId === null ? null : (known.current.get(albumId) ?? (fetched && String(fetched.id) === albumId ? fetched : null));

  const openArtist = useCallback(
    (id: number | string): void => {
      const current = routeRef.current;
      const target = String(id);
      if (current.detail?.albumId) {
        // Leaving the album modal for an artist: the artist replaces the album entry (or, when we are already on that
        // artist, simply closes the modal).
        if (current.detail.artistId === target) closeAlbum();
        else navigate(withDetail(current, { artistId: target }), { replace: true });
        return;
      }
      navigate(withDetail(current, { artistId: target }));
    },
    [navigate, closeAlbum]
  );

  const openCollection = useCallback(
    (id: string): void => navigate(withDetail(routeRef.current, { collectionId: id })),
    [navigate]
  );

  const openAlbum = useCallback(
    (item: AlbumItem): void => {
      const id = String(item.id);
      known.current.set(id, item);
      const detail = routeRef.current.detail;
      navigate(withDetail(routeRef.current, { ...detail, albumId: id }));
    },
    [navigate]
  );

  const closeDetail = useCallback((): void => navigateUp(withoutDetail(routeRef.current)), [navigateUp]);

  return { artistId, collectionId, albumId, album, openArtist, openCollection, openAlbum, closeAlbum, closeDetail };
}
