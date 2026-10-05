import { useCallback } from 'react';
import type { CustomFormat, CustomFormatImportResult, CustomFormatInput } from '@/types/customFormats';
import {
  createCustomFormat,
  deleteCustomFormat,
  exportCustomFormat,
  importCustomFormats,
  listCustomFormats,
  updateCustomFormat,
} from '@/services/customFormatService';
import { errorMessage } from '@/services/apiClient';
import { useLoadedList } from './useLoadedList';

export interface ImportOutcome {
  result: CustomFormatImportResult | null;
  /** Set when the whole import failed (bad JSON shape, nothing importable, request error). */
  error: string | null;
}

export interface UseCustomFormatsReturn {
  formats: CustomFormat[];
  loading: boolean;
  /** Creates (null id) or updates a format; resolves an inline error message, or null on success. */
  save: (id: number | null, input: CustomFormatInput) => Promise<string | null>;
  remove: (id: number) => Promise<void>;
  /** Parses and imports pasted/uploaded JSON text. */
  importText: (text: string) => Promise<ImportOutcome>;
  /** Pretty-printed Lidarr-schema JSON for one format, or null on failure. */
  exportText: (id: number) => Promise<string | null>;
  reload: () => Promise<void>;
}

type Toast = (msg: string, tone?: 'ok' | 'error') => void;

/** Custom formats: CRUD plus Lidarr-schema JSON import/export. */
export function useCustomFormats(enabled: boolean, onToast: Toast): UseCustomFormatsReturn {
  const { items, loading, reload } = useLoadedList<CustomFormat>(
    enabled,
    listCustomFormats,
    'Failed to load custom formats',
    onToast
  );

  const save = useCallback(
    async (id: number | null, input: CustomFormatInput): Promise<string | null> => {
      try {
        if (id === null) await createCustomFormat(input);
        else await updateCustomFormat(id, input);
        await reload();
        onToast(id === null ? 'Custom format created' : 'Custom format saved');
        return null;
      } catch (err: unknown) {
        return errorMessage(err, 'Failed to save custom format');
      }
    },
    [reload, onToast]
  );

  const remove = useCallback(
    async (id: number): Promise<void> => {
      try {
        await deleteCustomFormat(id);
        await reload();
        onToast('Custom format deleted');
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to delete custom format'), 'error');
      }
    },
    [reload, onToast]
  );

  const importText = useCallback(
    async (text: string): Promise<ImportOutcome> => {
      let payload: unknown;
      try {
        payload = JSON.parse(text);
      } catch (err: unknown) {
        return { result: null, error: `Not valid JSON: ${errorMessage(err, 'parse error')}` };
      }
      try {
        const result = await importCustomFormats(payload);
        await reload();
        return { result, error: null };
      } catch (err: unknown) {
        return { result: null, error: errorMessage(err, 'Import failed') };
      }
    },
    [reload]
  );

  const exportText = useCallback(
    async (id: number): Promise<string | null> => {
      try {
        return JSON.stringify(await exportCustomFormat(id), null, 2);
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to export custom format'), 'error');
        return null;
      }
    },
    [onToast]
  );

  return { formats: items, loading, save, remove, importText, exportText, reload };
}
