import { apiRequest } from './apiClient';
import type { Schema } from '@/types/apiSchema';
import type {
  ArtistProfile,
  ArtistProfileAlbum,
  ArtistProfileLibraryOnly,
  ArtistProfileTarget,
  ArtistProfileTrack,
  DiscoveryItem,
  DiscoveryStatus,
  DiscoveryTrackDetail,
} from '@/types/models';
import { isDiscoveryStatus } from '@/types/models';

/** Server items carry `item_type`; the UI reads `type`. Fall back to the id prefix, then album. */
function normalizeItem(raw: Schema<'DiscoveryItem'>): DiscoveryItem {
  const fromId = raw.id.split(':')[1];
  const kind = raw.item_type ?? fromId;
  return { ...raw, type: kind === 'track' || kind === 'artist' ? kind : 'album' };
}

function normalizeItems(items: Schema<'DiscoveryItem'>[] | undefined): DiscoveryItem[] {
  return (items || []).map(normalizeItem);
}

export async function getTrending(): Promise<DiscoveryItem[]> {
  const res = await apiRequest<{ items: Schema<'DiscoveryItem'>[] }>('/api/discovery/trending');
  return normalizeItems(res.items);
}

export async function getNewReleases(): Promise<DiscoveryItem[]> {
  const res = await apiRequest<{ items: Schema<'DiscoveryItem'>[] }>('/api/discovery/new-releases');
  return normalizeItems(res.items);
}

export async function search(query: string, type: string = 'all'): Promise<DiscoveryItem[]> {
  const params = new URLSearchParams({ q: query });
  if (type && type !== 'all') {
    params.set('type', type);
  }
  const res = await apiRequest<{ items: Schema<'DiscoveryItem'>[] }>(`/api/discovery/search?${params.toString()}`);
  return normalizeItems(res.items);
}

export async function getDiscoveryAlbumDetail(albumId: string): Promise<Record<string, unknown>> {
  return apiRequest<Record<string, unknown>>(`/api/discovery/album/${encodeURIComponent(albumId)}`);
}

export async function getDiscoveryTrackDetail(trackId: string): Promise<DiscoveryTrackDetail> {
  return apiRequest<DiscoveryTrackDetail>(`/api/discovery/track/${encodeURIComponent(trackId)}`);
}

function toStatus(value: string | null | undefined): DiscoveryStatus {
  return isDiscoveryStatus(value) ? value : 'none';
}

/** Discography rows are always addressable (the server omits the id only on library-only rows); an id-less row cannot be requested or opened. */
function toProfileAlbum(album: Schema<'ProfileAlbum'>): ArtistProfileAlbum[] {
  if (album.id == null) return [];
  return [{ ...album, id: album.id, artist: album.artist ?? '', status: toStatus(album.status) }];
}

function toLibraryOnly(album: Schema<'ProfileAlbum'>): ArtistProfileLibraryOnly {
  return { ...album, status: toStatus(album.status) };
}

function toProfileTrack(track: Schema<'ProfileTopTrack'>): ArtistProfileTrack[] {
  if (track.id == null) return [];
  return [{ ...track, id: track.id, title: track.title ?? '', status: toStatus(track.status) }];
}

function normalizeProfile(raw: Schema<'ArtistProfileResponse'>): ArtistProfile {
  const { top_tracks: tracks, discography, ...rest } = raw;
  return {
    ...rest,
    top_tracks: tracks.flatMap(toProfileTrack),
    discography: {
      albums: discography.albums.flatMap(toProfileAlbum),
      singles_eps: discography.singles_eps.flatMap(toProfileAlbum),
      compilations: discography.compilations.flatMap(toProfileAlbum),
      library_only: discography.library_only.map(toLibraryOnly),
    },
  };
}

export async function getArtistProfile(target: ArtistProfileTarget): Promise<ArtistProfile> {
  const params = new URLSearchParams();
  if ('discoveryId' in target) params.set('discovery_id', target.discoveryId);
  else params.set('library_artist_id', target.libraryArtistId);
  const raw = await apiRequest<Schema<'ArtistProfileResponse'>>(`/api/discovery/artist-profile?${params.toString()}`);
  return normalizeProfile(raw);
}
