import { useCallback } from 'react';
import type { QualityDefinition, QualityDefinitionInput } from '@/types/qualityDefinitions';
import {
  listQualityDefinitions,
  resetAllQualityDefinitions,
  resetQualityDefinition,
  updateQualityDefinition,
} from '@/services/qualityDefinitionService';
import { errorMessage } from '@/services/apiClient';
import { useLoadedList } from './useLoadedList';

export interface UseQualityDefinitionsReturn {
  definitions: QualityDefinition[];
  loading: boolean;
  /** Saves one row; resolves an error message for inline display, or null on success. */
  saveRow: (quality: string, input: QualityDefinitionInput) => Promise<string | null>;
  resetRow: (quality: string) => Promise<string | null>;
  resetAll: () => Promise<boolean>;
}

type Toast = (msg: string, tone?: 'ok' | 'error') => void;

/** Quality definitions (kbps bounds per quality) with per-row save/reset. */
export function useQualityDefinitions(enabled: boolean, onToast: Toast): UseQualityDefinitionsReturn {
  const { items, setItems, loading } = useLoadedList<QualityDefinition>(
    enabled,
    listQualityDefinitions,
    'Failed to load quality definitions',
    onToast
  );

  const replaceRow = useCallback(
    (row: QualityDefinition): void => {
      setItems((prev) => prev.map((d) => (d.quality === row.quality ? row : d)));
    },
    [setItems]
  );

  const saveRow = useCallback(
    async (quality: string, input: QualityDefinitionInput): Promise<string | null> => {
      try {
        replaceRow(await updateQualityDefinition(quality, input));
        return null;
      } catch (err: unknown) {
        return errorMessage(err, 'Failed to save quality definition');
      }
    },
    [replaceRow]
  );

  const resetRow = useCallback(
    async (quality: string): Promise<string | null> => {
      try {
        replaceRow(await resetQualityDefinition(quality));
        return null;
      } catch (err: unknown) {
        return errorMessage(err, 'Failed to reset quality definition');
      }
    },
    [replaceRow]
  );

  const resetAll = useCallback(async (): Promise<boolean> => {
    try {
      setItems(await resetAllQualityDefinitions());
      onToast('Quality definitions reset to defaults');
      return true;
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to reset quality definitions'), 'error');
      return false;
    }
  }, [setItems, onToast]);

  return { definitions: items, loading, saveRow, resetRow, resetAll };
}
