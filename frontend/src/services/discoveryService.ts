import { apiRequest } from './apiClient';
import type { ArtistDetail, DiscoveryItem } from '@/types/models';

export async function getTrending(): Promise<DiscoveryItem[]> {
  const res = await apiRequest<{ items: DiscoveryItem[] }>('/api/discovery/trending');
  return res.items || [];
}

export async function getNewReleases(): Promise<DiscoveryItem[]> {
  const res = await apiRequest<{ items: DiscoveryItem[] }>('/api/discovery/new-releases');
  return res.items || [];
}

export async function search(query: string, type: string = 'all'): Promise<DiscoveryItem[]> {
  const params = new URLSearchParams({ q: query });
  if (type && type !== 'all') {
    params.set('type', type);
  }
  const res = await apiRequest<{ items: DiscoveryItem[] }>(`/api/discovery/search?${params.toString()}`);
  return res.items || [];
}

export async function getDiscoveryAlbumDetail(albumId: string): Promise<Record<string, unknown>> {
  return apiRequest<Record<string, unknown>>(`/api/discovery/album/${encodeURIComponent(albumId)}`);
}

export async function getDiscoveryArtistDetail(artistId: string): Promise<ArtistDetail> {
  return apiRequest<ArtistDetail>(`/api/discovery/artist/${encodeURIComponent(artistId)}`);
}
