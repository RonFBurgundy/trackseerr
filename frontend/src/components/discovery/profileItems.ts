import type {
  ArtistDiscographyAlbum,
  ArtistProfileAlbum,
  ArtistProfileTrack,
  DiscoveryItem,
  DiscoveryStatus,
} from '@/types/models';

/** Statuses meaning the release is already (at least partly) in the library. */
export function isOwnedStatus(status: DiscoveryStatus | undefined): boolean {
  return status === 'in_library' || status === 'available';
}

export function isPendingStatus(status: DiscoveryStatus | undefined): boolean {
  return status === 'requested' || status === 'pending';
}

/** Status to display: a request made this session wins over a stale `missing`/`none`/`partial`/`rejected` from the server. */
export function effectiveStatus(
  status: DiscoveryStatus | undefined,
  id: string,
  requestedIds: ReadonlySet<string>
): DiscoveryStatus {
  const base = status ?? 'none';
  if (requestedIds.has(id) && !isOwnedStatus(base) && base !== 'processing') return 'requested';
  return base;
}

/** The discovery-album shape the tracklist modal and the request flow consume. */
export function profileAlbumToItem(album: ArtistProfileAlbum, status: DiscoveryStatus): DiscoveryItem {
  return {
    id: album.id,
    title: album.title,
    artist: album.artist,
    cover_url: album.cover_url ?? undefined,
    release_date: album.release_date ?? undefined,
    type: 'album',
    status,
    artist_discovery_id: album.artist_discovery_id ?? undefined,
  };
}

export function profileTrackToItem(track: ArtistProfileTrack, artist: string, cover?: string): DiscoveryItem {
  return {
    id: track.id,
    title: track.title,
    artist,
    album: track.album ?? undefined,
    cover_url: cover,
    type: 'track',
    preview_url: track.preview_url ?? undefined,
    status: track.status,
  };
}

export function profileAlbumToDiscography(album: ArtistProfileAlbum): ArtistDiscographyAlbum {
  return {
    id: album.id,
    title: album.title,
    artist: album.artist,
    cover_url: album.cover_url ?? undefined,
    release_date: album.release_date ?? undefined,
    record_type: album.record_type ?? undefined,
  };
}

/** Albums and singles/EPs we do not own and nobody has asked for yet: what "request everything missing" sends. */
export function missingReleases(albums: readonly ArtistProfileAlbum[], requestedIds: ReadonlySet<string>): ArtistProfileAlbum[] {
  return albums.filter((a) => {
    const status = effectiveStatus(a.status, a.id, requestedIds);
    return status === 'missing' || status === 'none' || status === 'rejected';
  });
}

export function releaseYear(date: string | null | undefined): string | undefined {
  return date ? date.slice(0, 4) : undefined;
}
