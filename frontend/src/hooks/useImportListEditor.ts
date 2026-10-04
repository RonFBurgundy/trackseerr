import { useCallback, useState } from 'react';
import {
  importListToInput,
  type ImportList,
  type ImportListConfig,
  type ImportListInput,
  type ImportListTestResult,
  type ListMonitorMode,
  type ProviderMeta,
} from '@/types/importLists';
import type { MonitorOption } from '@/types/monitoring';
import { createImportList, updateImportList, testImportList } from '@/services/importListsService';
import { errorMessage } from '@/services/apiClient';

export interface UseImportListEditorReturn {
  isOpen: boolean;
  editingId: string | null;
  draft: ImportListInput;
  provider: ProviderMeta | undefined;
  testResult: ImportListTestResult | null;
  testing: boolean;
  saving: boolean;
  error: string | null;
  openNew: () => void;
  openEdit: (list: ImportList) => void;
  close: () => void;
  patch: (changes: Partial<ImportListInput>) => void;
  setConfigValue: (key: string, value: string | number) => void;
  setProvider: (provider: string) => void;
  setMonitorMode: (mode: ListMonitorMode) => void;
  runTest: () => Promise<void>;
  save: () => Promise<void>;
}

function defaultConfig(meta: ProviderMeta | undefined): ImportListConfig {
  const cfg: ImportListConfig = {};
  if (!meta) return cfg;
  for (const f of meta.fields) {
    cfg[f.key] = f.type === 'select' && f.options && f.options.length > 0 ? f.options[0] : '';
  }
  return cfg;
}

/** Drops empty optional values and enforces required fields; returns an error message when invalid. */
function cleanDraft(draft: ImportListInput, meta: ProviderMeta | undefined): { input: ImportListInput } | { error: string } {
  if (!draft.name.trim()) return { error: 'Name is required' };
  if (!meta) return { error: 'Choose a provider' };
  const config: ImportListConfig = {};
  for (const f of meta.fields) {
    const v = draft.config[f.key];
    const empty = v === undefined || v === '';
    if (empty) {
      if (f.required) return { error: `${f.label} is required` };
      continue;
    }
    config[f.key] = f.type === 'number' ? Number(v) : v;
  }
  return {
    input: {
      ...draft,
      name: draft.name.trim(),
      config,
      artist_monitor_option: draft.monitor_mode === 'artist' ? draft.artist_monitor_option : null,
    },
  };
}

export function useImportListEditor(
  providers: ProviderMeta[],
  addMonitorOption: MonitorOption,
  onSaved: () => Promise<void>,
  onToast: (msg: string, tone?: 'ok' | 'error') => void
): UseImportListEditorReturn {
  const [isOpen, setIsOpen] = useState<boolean>(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<ImportListInput>({
    name: '',
    provider: '',
    config: {},
    enabled: true,
    monitor_mode: 'track',
    artist_monitor_option: null,
    quality_profile_id: null,
    sync_interval_minutes: 1440,
  });
  const [testResult, setTestResult] = useState<ImportListTestResult | null>(null);
  const [testing, setTesting] = useState<boolean>(false);
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const provider = providers.find((p) => p.provider === draft.provider);

  const openNew = useCallback((): void => {
    const first = providers[0];
    setDraft({
      name: '',
      provider: first?.provider ?? '',
      config: defaultConfig(first),
      enabled: true,
      monitor_mode: 'track',
      artist_monitor_option: null,
      quality_profile_id: null,
      sync_interval_minutes: 1440,
    });
    setEditingId(null);
    setTestResult(null);
    setError(null);
    setIsOpen(true);
  }, [providers]);

  const openEdit = useCallback((list: ImportList): void => {
    setDraft(importListToInput(list));
    setEditingId(list.id);
    setTestResult(null);
    setError(null);
    setIsOpen(true);
  }, []);

  const close = useCallback((): void => setIsOpen(false), []);

  const patch = useCallback((changes: Partial<ImportListInput>): void => {
    setDraft((prev) => ({ ...prev, ...changes }));
    setTestResult(null);
  }, []);

  const setConfigValue = useCallback((key: string, value: string | number): void => {
    setDraft((prev) => ({ ...prev, config: { ...prev.config, [key]: value } }));
    setTestResult(null);
  }, []);

  const setProvider = useCallback(
    (id: string): void => {
      const meta = providers.find((p) => p.provider === id);
      setDraft((prev) => ({ ...prev, provider: id, config: defaultConfig(meta) }));
      setTestResult(null);
    },
    [providers]
  );

  const setMonitorMode = useCallback(
    (mode: ListMonitorMode): void => {
      setDraft((prev) => ({
        ...prev,
        monitor_mode: mode,
        artist_monitor_option: mode === 'artist' ? prev.artist_monitor_option ?? addMonitorOption : null,
      }));
      setTestResult(null);
    },
    [addMonitorOption]
  );

  const runTest = useCallback(async (): Promise<void> => {
    const cleaned = cleanDraft(draft, provider);
    if ('error' in cleaned) {
      setError(cleaned.error);
      return;
    }
    setError(null);
    setTesting(true);
    try {
      setTestResult(await testImportList(cleaned.input, editingId));
    } catch (err: unknown) {
      setTestResult({ ok: false, item_count: 0, sample: [], error: errorMessage(err, 'Test failed') });
    } finally {
      setTesting(false);
    }
  }, [draft, provider, editingId]);

  const save = useCallback(async (): Promise<void> => {
    const cleaned = cleanDraft(draft, provider);
    if ('error' in cleaned) {
      setError(cleaned.error);
      return;
    }
    setError(null);
    setSaving(true);
    try {
      if (editingId) await updateImportList(editingId, cleaned.input);
      else await createImportList(cleaned.input);
      onToast(editingId ? 'Import list updated' : 'Import list created');
      setIsOpen(false);
      await onSaved();
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to save import list'));
    } finally {
      setSaving(false);
    }
  }, [draft, provider, editingId, onSaved, onToast]);

  return {
    isOpen, editingId, draft, provider, testResult, testing, saving, error,
    openNew, openEdit, close, patch, setConfigValue, setProvider, setMonitorMode, runTest, save,
  };
}
