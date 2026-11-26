import { apiRequest } from './apiClient';
import type { Schema } from '@/types/apiSchema';
import type { ServerPlaylistSummary, ServerPlaylistTrack } from '@/types/mediaServer';

function userQuery(user?: string): string {
  if (!user) return '';
  return `?${new URLSearchParams({ user }).toString()}`;
}

const serverPlaylistsBase = (serverId: string): string =>
  `/api/media-servers/${encodeURIComponent(serverId)}/playlists`;

export async function listServerPlaylists(
  serverId: string,
  user?: string
): Promise<ServerPlaylistSummary[]> {
  return (
    (await apiRequest<ServerPlaylistSummary[]>(`${serverPlaylistsBase(serverId)}${userQuery(user)}`)) ||
    []
  );
}

export async function getServerPlaylistItems(
  serverId: string,
  pid: string,
  user?: string
): Promise<ServerPlaylistTrack[]> {
  return (
    (await apiRequest<ServerPlaylistTrack[]>(
      `${serverPlaylistsBase(serverId)}/${encodeURIComponent(pid)}/items${userQuery(user)}`
    )) || []
  );
}

export async function adoptServerPlaylist(
  serverId: string,
  pid: string,
  user?: string
): Promise<Schema<'PlaylistRecord'>> {
  return apiRequest<Schema<'PlaylistRecord'>>(
    `${serverPlaylistsBase(serverId)}/${encodeURIComponent(pid)}/adopt${userQuery(user)}`,
    { method: 'POST' }
  );
}
