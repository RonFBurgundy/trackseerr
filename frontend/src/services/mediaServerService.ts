import { apiRequest } from './apiClient';
import type {
  MediaServerSettings,
  MediaServerSettingsInput,
  MediaServerStatus,
  MediaServerTestResult,
  MediaServerType,
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
