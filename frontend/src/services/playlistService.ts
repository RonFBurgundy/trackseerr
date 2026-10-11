import type { Schema } from '@/types/apiSchema';
import { apiRequest } from './apiClient';
import type { Playlist, FeaturedChart } from '@/types/models';
import type { ListMonitorMode } from '@/types/importLists';

export async function getPlaylists(): Promise<Playlist[]> {
  const res = await apiRequest<Playlist[]>('/api/playlists');
  return res || [];
}

export async function getPlaylistTracks(playlistId: string): Promise<Schema<'PlaylistTracksResponse'>> {
  return apiRequest<Schema<'PlaylistTracksResponse'>>(`/api/playlists/${encodeURIComponent(playlistId)}/tracks`);
}

export async function toggleUserTarget(playlistId: number | string, targetUserIds: string[]): Promise<void> {
  await apiRequest<void>(`/api/playlists/${playlistId}/targets`, {
    method: 'PUT',
    body: { user_ids: targetUserIds },
  });
}

export async function togglePlaylistActive(playlistId: number | string, isEnabled: boolean): Promise<void> {
  await apiRequest<void>(`/api/playlists/${playlistId}/enabled`, {
    method: 'PUT',
    body: { enabled: isEnabled },
  });
}

/** The playlist update call (same endpoint as the enable toggle); `monitor_mode` is optional. */
export async function updatePlaylistSettings(
  playlistId: number | string,
  settings: { enabled: boolean; monitor_mode?: ListMonitorMode }
): Promise<void> {
  await apiRequest<void>(`/api/playlists/${playlistId}/enabled`, {
    method: 'PUT',
    body: settings,
  });
}

export async function deletePlaylist(playlistId: number | string): Promise<void> {
  await apiRequest<void>(`/api/playlists/${playlistId}`, {
    method: 'DELETE',
  });
}

export async function triggerSync(): Promise<Schema<'SyncTriggerResponse'>> {
  return apiRequest<Schema<'SyncTriggerResponse'>>('/api/sync', {
    method: 'POST',
  });
}

export async function getFeaturedCharts(): Promise<FeaturedChart[]> {
  const res = await apiRequest<FeaturedChart[]>('/api/playlists/featured');
  return res || [];
}

/** A Spotify or Deezer playlist by link, a pasted list of `Artist - Title` lines, or an M3U file upload. */
export type ImportPlaylistPayload =
  | { source: 'link'; url: string; service?: string }
  | { source: 'tracks'; name: string; tracks: string[] }
  | { source: 'm3u'; name: string; content: string };

/** `Artist - Title` (first separator wins); a line with no separator is taken as a bare title. */
function parseTrackLine(line: string): { title: string; artist: string } {
  const at = line.indexOf(' - ');
  if (at < 0) return { title: line, artist: '' };
  return { artist: line.slice(0, at).trim(), title: line.slice(at + 3).trim() };
}

/** Resolves to the created playlist for a link import (it carries the fetched tracklist), otherwise null. */
export async function importPlaylist(payload: ImportPlaylistPayload): Promise<Schema<'PlaylistRecord'> | null> {
  if (payload.source === 'link') {
    return apiRequest<Schema<'PlaylistRecord'>>('/api/playlists', {
      method: 'POST',
      body: { url_or_id: payload.url, ...(payload.service ? { service: payload.service } : {}) },
    });
  }
  if (payload.source === 'm3u') {
    await apiRequest<Schema<'PlaylistImportResponse'>>('/api/playlists/import/m3u', {
      method: 'POST',
      body: { name: payload.name, content: payload.content },
    });
    return null;
  }
  await apiRequest<Schema<'PlaylistImportResponse'>>('/api/playlists/import', {
    method: 'POST',
    body: { name: payload.name, service: 'custom', tracks: payload.tracks.map(parseTrackLine) },
  });
  return null;
}
