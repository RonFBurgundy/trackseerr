import type { Schema } from '@/types/apiSchema';
import { apiRequest } from './apiClient';
import type {
  ImportList,
  ImportListInput,
  ImportListItemsPage,
  ImportListItemStatus,
  ImportListTestResult,
  ProviderMeta,
} from '@/types/importLists';

export async function getImportListProviders(): Promise<ProviderMeta[]> {
  const res = await apiRequest<ProviderMeta[]>('/api/import-lists/providers');
  return res || [];
}

export async function getImportLists(): Promise<ImportList[]> {
  const res = await apiRequest<ImportList[]>('/api/import-lists');
  return res || [];
}

export async function createImportList(input: ImportListInput): Promise<ImportList> {
  return apiRequest<ImportList>('/api/import-lists', { method: 'POST', body: input });
}

export async function updateImportList(id: string, input: ImportListInput): Promise<ImportList> {
  return apiRequest<ImportList>(`/api/import-lists/${encodeURIComponent(id)}`, { method: 'PUT', body: input });
}

export async function deleteImportList(id: string): Promise<void> {
  await apiRequest<void>(`/api/import-lists/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

/** Queues a sync; the server answers 409 (surfaced as an ApiError) when one is already running. */
export async function syncImportList(id: string): Promise<Schema<'ImportListQueued'>> {
  return apiRequest<Schema<'ImportListQueued'>>(`/api/import-lists/${encodeURIComponent(id)}/sync`, { method: 'POST' });
}

/**
 * Dry-runs a list. Pass `listId` when editing a stored list so its masked secrets (********) resolve to the
 * stored values server-side; the server ignores them if the provider was changed.
 */
export async function testImportList(input: ImportListInput, listId?: string | null): Promise<ImportListTestResult> {
  const qs = listId ? `?list_id=${encodeURIComponent(listId)}` : '';
  return apiRequest<ImportListTestResult>(`/api/import-lists/test${qs}`, { method: 'POST', body: input });
}

export async function getImportListItems(
  id: string,
  status: ImportListItemStatus | null,
  limit: number,
  offset: number
): Promise<ImportListItemsPage> {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (status) qs.set('status', status);
  return apiRequest<ImportListItemsPage>(`/api/import-lists/${encodeURIComponent(id)}/items?${qs.toString()}`);
}
