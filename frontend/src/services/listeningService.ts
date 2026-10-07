import type { Schema } from '@/types/apiSchema';
import type { ListeningPlaylistPayload, ListeningSources } from '@/types/listening';
import { PERMISSION_AUTO_REQUEST_PLAYLISTS } from '@/types/listening';
import { apiRequest } from './apiClient';

export async function getListeningSources(): Promise<ListeningSources> {
  return apiRequest<ListeningSources>('/api/playlists/listening/sources');
}

export async function createListeningPlaylist(
  payload: ListeningPlaylistPayload
): Promise<Schema<'PlaylistImportResponse'>> {
  return apiRequest<Schema<'PlaylistImportResponse'>>('/api/playlists/listening', {
    method: 'POST',
    body: payload,
  });
}

export async function setPlaylistAutoRequest(playlistId: number | string, autoRequest: boolean): Promise<void> {
  await apiRequest<Schema<'PlaylistAutoRequestResponse'>>(`/api/playlists/${playlistId}/auto-request`, {
    method: 'PUT',
    body: { auto_request: autoRequest },
  });
}

/** Whether the signed-in user may acquire music from playlists automatically: an admin, or holder of the permission bit. */
export async function getCanAutoRequestPlaylists(): Promise<boolean> {
  const me = await apiRequest<Schema<'CurrentUserProfile'>>('/api/users/me');
  return me.is_admin || (me.permissions & PERMISSION_AUTO_REQUEST_PLAYLISTS) !== 0;
}
