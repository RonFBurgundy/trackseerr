import { apiRequest } from './apiClient';
import type { Narrow, Schema } from '@/types/apiSchema';
import type {
  GeneralSettings,
  DownloadClientItem,
  DownloadClientRoots,
  IndexerItem,
  IndexerPayload,
  TestIndexerPayload,
  SystemStatusInfo,
  MediaManagementSettings,
  RecycleBinEmptyResult,
  LidarrSettings,
  LidarrTestResult,
  LibraryManagerMode,
  LibraryManagerState,
  LidarrDefaults,
} from '@/types/models';

export async function getGeneralSettings(): Promise<GeneralSettings> {
  return apiRequest<GeneralSettings>('/api/settings/general');
}

export async function updateGeneralSettings(settings: Schema<'GeneralSettingsUpdateModel'>): Promise<GeneralSettings> {
  return apiRequest<GeneralSettings>('/api/settings/general', {
    method: 'POST',
    body: settings,
  });
}

export async function getClientSettings(): Promise<DownloadClientItem[]> {
  const res = await apiRequest<DownloadClientItem[]>('/api/settings/download-clients');
  return res || [];
}

export async function getDownloadClientRoots(refresh = false): Promise<DownloadClientRoots[]> {
  const res = await apiRequest<DownloadClientRoots[]>(
    `/api/settings/download-clients/roots${refresh ? '?refresh=true' : ''}`
  );
  return res || [];
}

export async function saveClientSettings(client: Schema<'DownloadClientPayload'>): Promise<DownloadClientItem> {
  return apiRequest<DownloadClientItem>('/api/settings/download-clients', {
    method: 'POST',
    body: client,
  });
}

export async function deleteClientSettings(clientId: string): Promise<void> {
  await apiRequest<void>(`/api/settings/download-clients/${clientId}`, {
    method: 'DELETE',
  });
}

export async function testClientConnection(client: Schema<'TestConnectionPayload'>): Promise<Schema<'TestConnectionResponse'>> {
  return apiRequest<Schema<'TestConnectionResponse'>>('/api/settings/download-clients/test', {
    method: 'POST',
    body: client,
  });
}

export async function getIndexerSettings(): Promise<IndexerItem[]> {
  const res = await apiRequest<IndexerItem[]>('/api/settings/indexers');
  return res || [];
}

export async function saveIndexer(indexer: IndexerPayload): Promise<IndexerItem> {
  return apiRequest<IndexerItem>('/api/settings/indexers', {
    method: 'POST',
    body: indexer,
  });
}

export async function deleteIndexer(indexerId: string): Promise<void> {
  await apiRequest<void>(`/api/settings/indexers/${indexerId}`, {
    method: 'DELETE',
  });
}

export async function testIndexer(indexer: TestIndexerPayload): Promise<Schema<'TestIndexerResponse'>> {
  return apiRequest<Schema<'TestIndexerResponse'>>('/api/settings/indexers/test', {
    method: 'POST',
    body: indexer,
  });
}

export async function getSystemStatus(): Promise<SystemStatusInfo> {
  return apiRequest<SystemStatusInfo>('/api/system/status');
}

export async function getMediaManagementSettings(): Promise<MediaManagementSettings> {
  const res = await apiRequest<Narrow<Schema<'MediaManagementGetResponse'>, { settings: MediaManagementSettings }>>('/api/settings/media-management');
  // `seed_rule_conflict` is computed server-side and sent beside `settings`, not inside it.
  return { ...res.settings, seed_rule_conflict: res.seed_rule_conflict };
}

export async function updateMediaManagementSettings(
  settings: Partial<MediaManagementSettings>
): Promise<MediaManagementSettings> {
  return apiRequest<MediaManagementSettings>('/api/settings/media-management', {
    method: 'POST',
    body: settings,
  });
}

/** Permanently deletes everything in the recycle bin (the server requires the explicit confirm flag). */
export async function emptyRecycleBin(): Promise<RecycleBinEmptyResult> {
  return apiRequest<RecycleBinEmptyResult>('/api/recycle-bin/empty', {
    method: 'POST',
    body: { confirm: true },
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
export async function getLidarrDefaults(): Promise<LidarrDefaults> {
  return apiRequest<LidarrDefaults>('/api/settings/lidarr/defaults');
}

export async function getApiKey(): Promise<Schema<'ApiKeyResponse'>> {
  return apiRequest<Schema<'ApiKeyResponse'>>('/api/settings/api-key');
}

export async function regenerateApiKey(): Promise<Schema<'ApiKeyRegenerateResponse'>> {
  return apiRequest<Schema<'ApiKeyRegenerateResponse'>>('/api/settings/api-key/regenerate', {
    method: 'POST',
  });
}
