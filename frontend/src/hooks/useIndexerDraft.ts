import { useCallback, useState } from 'react';
import type { IndexerItem } from '@/types/models';

/** Seeding fields held as raw input text; '' means "inherit global" (null). */
export interface SeedingDraft {
  seed_ratio: string;
  seed_time_minutes: string;
  discography_seed_time_minutes: string;
  minimum_seeders: string;
}

export type SeedingDraftField = keyof SeedingDraft;

export const EMPTY_SEEDING_DRAFT: SeedingDraft = {
  seed_ratio: '',
  seed_time_minutes: '',
  discography_seed_time_minutes: '',
  minimum_seeders: '',
};

const toText = (v: number | null | undefined): string => (v === null || v === undefined ? '' : String(v));

/** '' -> null; otherwise a non-negative number (integers only when `integer`), or undefined when invalid. */
export function parseSeedValue(text: string, integer: boolean): number | null | undefined {
  const t = text.trim();
  if (t === '') return null;
  const n = Number(t);
  if (!Number.isFinite(n) || n < 0) return undefined;
  if (integer && !Number.isInteger(n)) return undefined;
  return n;
}

export type SeedingPayload = Pick<
  IndexerItem,
  'seed_ratio' | 'seed_time_minutes' | 'discography_seed_time_minutes' | 'minimum_seeders'
>;

/** Converts the draft to the API shape, or null when any field is invalid. */
export function seedingDraftToPayload(draft: SeedingDraft): SeedingPayload | null {
  const seed_ratio = parseSeedValue(draft.seed_ratio, false);
  const seed_time_minutes = parseSeedValue(draft.seed_time_minutes, true);
  const discography_seed_time_minutes = parseSeedValue(draft.discography_seed_time_minutes, true);
  const minimum_seeders = parseSeedValue(draft.minimum_seeders, true);
  if (
    seed_ratio === undefined ||
    seed_time_minutes === undefined ||
    discography_seed_time_minutes === undefined ||
    minimum_seeders === undefined
  ) {
    return null;
  }
  return { seed_ratio, seed_time_minutes, discography_seed_time_minutes, minimum_seeders };
}

export interface IndexerDraft {
  editingId: IndexerItem['id'] | null;
  name: string;
  url: string;
  apiKey: string;
  indexerType: IndexerItem['indexer_type'];
  seeding: SeedingDraft;
  setName: (v: string) => void;
  setUrl: (v: string) => void;
  setApiKey: (v: string) => void;
  setSeedingField: (field: SeedingDraftField, value: string) => void;
  startEdit: (indexer: IndexerItem) => void;
  reset: () => void;
}

/** Form state for adding or editing an indexer, including its seed rules. */
export function useIndexerDraft(): IndexerDraft {
  const [editingId, setEditingId] = useState<IndexerItem['id'] | null>(null);
  const [name, setName] = useState<string>('');
  const [url, setUrl] = useState<string>('');
  const [apiKey, setApiKey] = useState<string>('');
  const [indexerType, setIndexerType] = useState<IndexerItem['indexer_type']>('torznab');
  const [seeding, setSeeding] = useState<SeedingDraft>(EMPTY_SEEDING_DRAFT);

  const setSeedingField = useCallback((field: SeedingDraftField, value: string) => {
    setSeeding((prev) => ({ ...prev, [field]: value }));
  }, []);

  const startEdit = useCallback((indexer: IndexerItem) => {
    setEditingId(indexer.id);
    setName(indexer.name);
    setUrl(indexer.host_url);
    setApiKey('');
    setIndexerType(indexer.indexer_type);
    setSeeding({
      seed_ratio: toText(indexer.seed_ratio),
      seed_time_minutes: toText(indexer.seed_time_minutes),
      discography_seed_time_minutes: toText(indexer.discography_seed_time_minutes),
      minimum_seeders: toText(indexer.minimum_seeders),
    });
  }, []);

  const reset = useCallback(() => {
    setEditingId(null);
    setName('');
    setUrl('');
    setApiKey('');
    setIndexerType('torznab');
    setSeeding(EMPTY_SEEDING_DRAFT);
  }, []);

  return { editingId, name, url, apiKey, indexerType, seeding, setName, setUrl, setApiKey, setSeedingField, startEdit, reset };
}
