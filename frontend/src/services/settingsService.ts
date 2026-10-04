import { apiRequest } from './apiClient';
import type {
  GeneralSettings,
  QualityProfile,
  DownloadClientItem,
  IndexerItem,
  SystemStatusInfo,
  MediaManagementSettings,
  LidarrSettings,
  LidarrTestResult,
  LibraryManagerMode,
  LibraryManagerState,
  LidarrOptions,
} from '@/types/models';

export async function getGeneralSettings(): Promise<GeneralSettings> {
  return apiRequest<GeneralSettings>('/api/settings/general');
}

export async function updateGeneralSettings(settings: Partial<GeneralSettings>): Promise<GeneralSettings> {
  return apiRequest<GeneralSettings>('/api/settings/general', {
    method: 'POST',
    body: settings,
  });
}

export async function getQualityProfiles(): Promise<QualityProfile[]> {
  const res = await apiRequest<QualityProfile[]>('/api/settings/quality-profiles');
  return res || [];
}

export async function saveQualityProfile(profile: Partial<QualityProfile>): Promise<QualityProfile> {
  return apiRequest<QualityProfile>('/api/settings/quality-profiles', {
    method: 'POST',
    body: profile,
  });
}

export async function deleteQualityProfile(profileId: number): Promise<void> {
  await apiRequest<void>(`/api/settings/quality-profiles/${profileId}`, {
    method: 'DELETE',
  });
}

export async function getClientSettings(): Promise<DownloadClientItem[]> {
  const res = await apiRequest<DownloadClientItem[]>('/api/settings/download-clients');
  return res || [];
}

export async function saveClientSettings(client: Partial<DownloadClientItem>): Promise<DownloadClientItem> {
  return apiRequest<DownloadClientItem>('/api/settings/download-clients', {
    method: 'POST',
    body: client,
  });
}

export async function deleteClientSettings(clientId: number): Promise<void> {
  await apiRequest<void>(`/api/settings/download-clients/${clientId}`, {
    method: 'DELETE',
  });
}

export async function testClientConnection(client: Partial<DownloadClientItem>): Promise<{ success: boolean; message: string }> {
  return apiRequest<{ success: boolean; message: string }>('/api/settings/download-clients/test', {
    method: 'POST',
    body: client,
  });
}

export async function getIndexerSettings(): Promise<IndexerItem[]> {
  const res = await apiRequest<IndexerItem[]>('/api/settings/indexers');
  return res || [];
}

export async function saveIndexer(indexer: Partial<IndexerItem>): Promise<IndexerItem> {
  return apiRequest<IndexerItem>('/api/settings/indexers', {
    method: 'POST',
    body: indexer,
  });
}

export async function deleteIndexer(indexerId: number): Promise<void> {
  await apiRequest<void>(`/api/settings/indexers/${indexerId}`, {
    method: 'DELETE',
  });
}

export async function testIndexer(indexer: Partial<IndexerItem>): Promise<{ success: boolean; message: string }> {
  return apiRequest<{ success: boolean; message: string }>('/api/settings/indexers/test', {
    method: 'POST',
    body: indexer,
  });
}

export async function getSystemStatus(): Promise<SystemStatusInfo> {
  return apiRequest<SystemStatusInfo>('/api/system/status');
}

export async function getMediaManagementSettings(): Promise<MediaManagementSettings> {
  const res = await apiRequest<{ settings: MediaManagementSettings; presets?: Record<string, unknown> }>(
    '/api/settings/media-management'
  );
  return res.settings;
}

export async function updateMediaManagementSettings(
  settings: Partial<MediaManagementSettings>
): Promise<MediaManagementSettings> {
  return apiRequest<MediaManagementSettings>('/api/settings/media-management', {
    method: 'POST',
    body: settings,
  });
}

export async function getLidarrSettings(): Promise<LidarrSettings> {
  return apiRequest<LidarrSettings>('/api/settings/lidarr');
}

export async function updateLidarrSettings(
  settings: Partial<LidarrSettings>
): Promise<LidarrSettings> {
  return apiRequest<LidarrSettings>('/api/settings/lidarr', {
    method: 'POST',
    body: settings,
  });
}

export async function testLidarrConnection(payload: {
  url: string;
  api_key: string;
}): Promise<LidarrTestResult> {
  return apiRequest<LidarrTestResult>('/api/settings/lidarr/test', {
    method: 'POST',
    body: payload,
  });
}

export async function getLibraryManager(): Promise<LibraryManagerState> {
  return apiRequest<LibraryManagerState>('/api/settings/library-manager');
}

/** Rejects with ApiError: 409 (work in flight, message is the blocking reason) or 422 (Lidarr not configured). */
export async function setLibraryManager(mode: LibraryManagerMode): Promise<LibraryManagerState> {
  return apiRequest<LibraryManagerState>('/api/settings/library-manager', {
    method: 'PUT',
    body: { mode },
  });
}

/** Live from Lidarr; rejects with ApiError status 502 (redacted message) when Lidarr is unreachable. */
export async function getLidarrOptions(): Promise<LidarrOptions> {
  return apiRequest<LidarrOptions>('/api/settings/lidarr/options');
}
