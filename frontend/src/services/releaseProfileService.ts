import { apiRequest } from './apiClient';
import type {
  ReleaseProfile,
  ReleaseProfileDeleteResult,
  ReleaseProfileInput,
  ReleaseProfileList,
  ReleaseProfilePreview,
} from '@/types/releaseProfiles';

/** Native mode only: every route answers 409 while Lidarr manages the library. */
export async function listReleaseProfiles(): Promise<ReleaseProfileList> {
  return apiRequest<ReleaseProfileList>('/api/library/release-profiles');
}

export async function createReleaseProfile(input: ReleaseProfileInput): Promise<ReleaseProfile> {
  return apiRequest<ReleaseProfile>('/api/library/release-profiles', { method: 'POST', body: input });
}

export async function updateReleaseProfile(id: number, input: ReleaseProfileInput): Promise<ReleaseProfile> {
  return apiRequest<ReleaseProfile>(`/api/library/release-profiles/${id}`, { method: 'PUT', body: input });
}

export async function deleteReleaseProfile(id: number): Promise<ReleaseProfileDeleteResult> {
  return apiRequest<ReleaseProfileDeleteResult>(`/api/library/release-profiles/${id}`, { method: 'DELETE' });
}

export async function previewReleaseProfile(
  artistId: number | string,
  profileId: number | null,
  signal?: AbortSignal
): Promise<ReleaseProfilePreview> {
  return apiRequest<ReleaseProfilePreview>(
    `/api/library/artists/${artistId}/release-profile-preview${profileId === null ? '' : `?profile_id=${profileId}`}`,
    { signal }
  );
}

/** Sets (or, with null, clears) an artist's release profile; `applyToAlbums` recomputes its albums' monitoring. */
export async function setArtistReleaseProfile(
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
      release_profile_id: profileId,
      apply_monitor_to_albums: applyToAlbums,
    },
  });
}
