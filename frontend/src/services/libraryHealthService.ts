import { apiRequest } from './apiClient';
import type {
  LibraryHealthCountResponse,
  LibraryHealthDismissRequest,
  LibraryHealthMappingRequest,
  LibraryHealthResponse,
  LibraryHealthStarted,
  LibraryHealthWeeklyResponse,
} from '@/types/libraryHealth';

const BASE = '/api/library-health';

export function getLibraryHealth(signal?: AbortSignal): Promise<LibraryHealthResponse> {
  return apiRequest<LibraryHealthResponse>(BASE, { signal });
}

export function getLibraryHealthCount(signal?: AbortSignal): Promise<LibraryHealthCountResponse> {
  return apiRequest<LibraryHealthCountResponse>(`${BASE}/count`, { signal });
}

/** Starts a check; the server answers 409 (ApiError) when one is already running. */
export function startLibraryHealthCheck(): Promise<LibraryHealthStarted> {
  return apiRequest<LibraryHealthStarted>(`${BASE}/check`, { method: 'POST' });
}

export function dismissLibraryHealth(body: LibraryHealthDismissRequest): Promise<unknown> {
  return apiRequest<unknown>(`${BASE}/dismiss`, { method: 'POST', body });
}

export function setLibraryHealthMapping(body: LibraryHealthMappingRequest): Promise<unknown> {
  return apiRequest<unknown>(`${BASE}/mapping`, { method: 'PUT', body });
}

export function deleteLibraryHealthMapping(): Promise<unknown> {
  return apiRequest<unknown>(`${BASE}/mapping`, { method: 'DELETE' });
}

export function setLibraryHealthWeekly(enabled: boolean): Promise<LibraryHealthWeeklyResponse> {
  return apiRequest<LibraryHealthWeeklyResponse>(`${BASE}/weekly`, { method: 'PUT', body: { enabled } });
}
