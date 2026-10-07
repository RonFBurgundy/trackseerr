import type { Schema } from '@/types/apiSchema';
import { apiRequest } from './apiClient';
import type { Playlist } from '@/types/models';
import type { ListMonitorMode } from '@/types/importLists';

export async function getPlaylists(): Promise<Playlist[]> {
  const res = await apiRequest<Playlist[]>('/api/playlists');
  return res || [];
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

/** A Spotify or Deezer playlist by link, or a pasted list of `Artist - Title` lines with a name of the user's choosing. */
export type ImportPlaylistPayload =
  | { source: 'link'; url: string }
  | { source: 'tracks'; name: string; tracks: string[] };

/** `Artist - Title` (first separator wins); a line with no separator is taken as a bare title. */
function parseTrackLine(line: string): { title: string; artist: string } {
  const at = line.indexOf(' - ');
  if (at < 0) return { title: line, artist: '' };
  return { artist: line.slice(0, at).trim(), title: line.slice(at + 3).trim() };
}

export async function importPlaylist(payload: ImportPlaylistPayload): Promise<void> {
  if (payload.source === 'link') {
    await apiRequest<Schema<'PlaylistRecord'>>('/api/playlists', {
      method: 'POST',
      body: { url_or_id: payload.url },
    });
    return;
  }
  await apiRequest<Schema<'PlaylistImportResponse'>>('/api/playlists/import', {
    method: 'POST',
    body: { name: payload.name, service: 'custom', tracks: payload.tracks.map(parseTrackLine) },
  });
}
