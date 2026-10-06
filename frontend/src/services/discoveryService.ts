import { apiRequest } from './apiClient';
import type { ArtistProfile, ArtistProfileTarget, DiscoveryItem, DiscoveryTrackDetail } from '@/types/models';

/** Server items carry `item_type`; the UI reads `type`. Fall back to the id prefix, then album. */
function normalizeItem(raw: DiscoveryItem & { item_type?: string }): DiscoveryItem {
  if (raw.type === 'album' || raw.type === 'track' || raw.type === 'artist') return raw;
  const fromId = raw.id.split(':')[1];
  const kind = raw.item_type ?? fromId;
  return { ...raw, type: kind === 'track' || kind === 'artist' ? kind : 'album' };
}

function normalizeItems(items: DiscoveryItem[] | undefined): DiscoveryItem[] {
  return (items || []).map(normalizeItem);
}

export async function getTrending(): Promise<DiscoveryItem[]> {
  const res = await apiRequest<{ items: DiscoveryItem[] }>('/api/discovery/trending');
  return normalizeItems(res.items);
}

export async function getNewReleases(): Promise<DiscoveryItem[]> {
  const res = await apiRequest<{ items: DiscoveryItem[] }>('/api/discovery/new-releases');
  return normalizeItems(res.items);
}

export async function search(query: string, type: string = 'all'): Promise<DiscoveryItem[]> {
  const params = new URLSearchParams({ q: query });
  if (type && type !== 'all') {
    params.set('type', type);
  }
  const res = await apiRequest<{ items: DiscoveryItem[] }>(`/api/discovery/search?${params.toString()}`);
  return normalizeItems(res.items);
}

export async function getDiscoveryAlbumDetail(albumId: string): Promise<Record<string, unknown>> {
  return apiRequest<Record<string, unknown>>(`/api/discovery/album/${encodeURIComponent(albumId)}`);
}

export async function getDiscoveryTrackDetail(trackId: string): Promise<DiscoveryTrackDetail> {
  return apiRequest<DiscoveryTrackDetail>(`/api/discovery/track/${encodeURIComponent(trackId)}`);
}

export async function getArtistProfile(target: ArtistProfileTarget): Promise<ArtistProfile> {
  const params = new URLSearchParams();
  if ('discoveryId' in target) params.set('discovery_id', target.discoveryId);
  else params.set('library_artist_id', target.libraryArtistId);
  return apiRequest<ArtistProfile>(`/api/discovery/artist-profile?${params.toString()}`);
}
