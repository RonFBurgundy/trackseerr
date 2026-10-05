import { apiRequest } from './apiClient';
import type {
  MetadataProfile,
  MetadataProfileDeleteResult,
  MetadataProfileInput,
  MetadataProfileList,
  MetadataProfilePreview,
} from '@/types/metadataProfiles';

/** Native mode only: every route answers 409 while Lidarr manages the library. */
export async function listMetadataProfiles(): Promise<MetadataProfileList> {
  return apiRequest<MetadataProfileList>('/api/library/metadata-profiles');
}

export async function createMetadataProfile(input: MetadataProfileInput): Promise<MetadataProfile> {
  return apiRequest<MetadataProfile>('/api/library/metadata-profiles', { method: 'POST', body: input });
}

export async function updateMetadataProfile(id: number, input: MetadataProfileInput): Promise<MetadataProfile> {
  return apiRequest<MetadataProfile>(`/api/library/metadata-profiles/${id}`, { method: 'PUT', body: input });
}

export async function deleteMetadataProfile(id: number): Promise<MetadataProfileDeleteResult> {
  return apiRequest<MetadataProfileDeleteResult>(`/api/library/metadata-profiles/${id}`, { method: 'DELETE' });
}

export async function previewMetadataProfile(
  artistId: number | string,
  profileId: number | null,
  signal?: AbortSignal
): Promise<MetadataProfilePreview> {
  return apiRequest<MetadataProfilePreview>(
    `/api/library/artists/${artistId}/metadata-profile-preview${profileId === null ? '' : `?profile_id=${profileId}`}`,
    { signal }
  );
}

/** Sets (or, with null, clears) an artist's metadata profile; `applyToAlbums` recomputes its albums' monitoring. */
export async function setArtistMetadataProfile(
  artistId: number | string,
  monitored: boolean,
  profileId: number | null,
  applyToAlbums: boolean
): Promise<void> {
  await apiRequest<void>(`/api/library/artists/${artistId}/monitored`, {
    method: 'PUT',
    body: {
      monitored,
      cascade_children: false,
      metadata_profile_id: profileId,
      apply_monitor_to_albums: applyToAlbums,
    },
  });
}
