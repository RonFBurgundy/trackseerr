import { apiRequest } from './apiClient';
import type {
  MediaServerDeletedResponse,
  MediaServerItem,
  MediaServerLoginResponse,
  MediaServerPayload,
  MediaServerSettings,
  MediaServerSettingsInput,
  MediaServerStatus,
  MediaServerTestResult,
  MediaServerType,
  PlexAccountCheckResponse,
  PlexAccountDisconnectResponse,
  PlexPinResponse,
} from '@/types/mediaServer';

const MEDIA_SERVER_TYPES: readonly MediaServerType[] = ['plex', 'subsonic', 'jellyfin', 'none'];

function isMediaServerStatus(value: unknown): value is MediaServerStatus {
  if (typeof value !== 'object' || value === null) return false;
  const body = value as Partial<Record<keyof MediaServerStatus, unknown>>;
  const caps = body.capabilities;
  return (
    typeof body.type === 'string' &&
    (MEDIA_SERVER_TYPES as readonly string[]).includes(body.type) &&
    typeof body.connected === 'boolean' &&
    typeof caps === 'object' &&
    caps !== null
  );
}

/**
 * Reads the unauthenticated media-server endpoint (the login screen needs it before any session).
 * Resolves null when the server is unreachable, predates the endpoint, or answers with an unexpected body.
 */
export async function getMediaServerStatus(): Promise<MediaServerStatus | null> {
  try {
    const res = await apiRequest<unknown>('/api/system/media-server', { passthroughUnauthorized: true });
    return isMediaServerStatus(res) ? res : null;
  } catch (err: unknown) {
    if (err instanceof Error) return null;
    throw err;
  }
}

export async function getMediaServerSettings(): Promise<MediaServerSettings> {
  return apiRequest<MediaServerSettings>('/api/settings/media-server');
}

export async function saveMediaServerSettings(input: MediaServerSettingsInput): Promise<MediaServerSettings> {
  return apiRequest<MediaServerSettings>('/api/settings/media-server', { method: 'PUT', body: input });
}

export async function testMediaServerSettings(input: MediaServerSettingsInput): Promise<MediaServerTestResult> {
  return apiRequest<MediaServerTestResult>('/api/settings/media-server/test', { method: 'POST', body: input });
}

export async function startPlexAccountSignIn(): Promise<PlexPinResponse> {
  return apiRequest<PlexPinResponse>('/api/settings/media-server/plex-account/pin', { method: 'POST' });
}

export async function checkPlexAccountSignIn(pinId: number): Promise<PlexAccountCheckResponse> {
  return apiRequest<PlexAccountCheckResponse>('/api/settings/media-server/plex-account/check', {
    method: 'POST',
    body: { pin_id: pinId },
  });
}

export async function disconnectPlexAccount(): Promise<PlexAccountDisconnectResponse> {
  return apiRequest<PlexAccountDisconnectResponse>('/api/settings/media-server/plex-account', { method: 'DELETE' });
}

export async function listMediaServers(): Promise<MediaServerItem[]> {
  return apiRequest<MediaServerItem[]>('/api/settings/media-servers');
}

export async function saveMediaServer(payload: MediaServerPayload): Promise<MediaServerItem> {
  return apiRequest<MediaServerItem>('/api/settings/media-servers', { method: 'POST', body: payload });
}

export async function testMediaServer(payload: MediaServerPayload): Promise<MediaServerTestResult> {
  return apiRequest<MediaServerTestResult>('/api/settings/media-servers/test', { method: 'POST', body: payload });
}

export async function deleteMediaServer(id: string): Promise<MediaServerDeletedResponse> {
  return apiRequest<MediaServerDeletedResponse>(`/api/settings/media-servers/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
}

export async function setLoginMediaServer(id: string | null): Promise<MediaServerLoginResponse> {
  return apiRequest<MediaServerLoginResponse>('/api/settings/media-servers/login', {
    method: 'PUT',
    body: { server_id: id },
  });
}
