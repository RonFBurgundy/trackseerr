import { useCallback, useEffect, useState } from 'react';
import type { ListMonitorMode } from '@/types/importLists';
import type {
  ItunesImportStatus,
  ItunesPathMapping,
  ItunesPreview,
} from '@/types/itunesImport';
import { commitItunesImport, getItunesImportStatus, previewItunesImport } from '@/services/itunesImportService';
import { errorMessage } from '@/services/apiClient';
import { usePolling } from './usePolling';

export type ItunesStage = 'upload' | 'review' | 'running' | 'done';

export interface UseItunesImportReturn {
  stage: ItunesStage;
  busy: boolean;
  error: string | null;
  preview: ItunesPreview | null;
  selected: ReadonlySet<string>;
  mappings: ItunesPathMapping[];
  monitorMode: ListMonitorMode;
  namePrefix: string;
  includeFolders: boolean;
  importPlayStats: boolean;
  status: ItunesImportStatus | null;
  upload: (file: File) => Promise<void>;
  toggle: (key: string) => void;
  setAll: (on: boolean) => void;
  setMapping: (index: number, patch: Partial<ItunesPathMapping>) => void;
  addMapping: () => void;
  removeMapping: (index: number) => void;
  setMonitorMode: (mode: ListMonitorMode) => void;
  setNamePrefix: (value: string) => void;
  setIncludeFolders: (value: boolean) => void;
  setImportPlayStats: (value: boolean) => void;
  start: () => Promise<void>;
  reset: () => void;
}

const POLL_MS = 1500;

export function useItunesImport(): UseItunesImportReturn {
  const [stage, setStage] = useState<ItunesStage>('upload');
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<ItunesPreview | null>(null);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set<string>());
  const [mappings, setMappings] = useState<ItunesPathMapping[]>([]);
  const [monitorMode, setMonitorMode] = useState<ListMonitorMode>('none');
  const [namePrefix, setNamePrefix] = useState<string>('');
  const [includeFolders, setIncludeFolders] = useState<boolean>(false);
  const [importPlayStats, setImportPlayStats] = useState<boolean>(false);
  const [status, setStatus] = useState<ItunesImportStatus | null>(null);

  const upload = useCallback(async (file: File): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const result = await previewItunesImport(file);
      setPreview(result);
      setSelected(new Set(result.playlists.map((p) => p.key)));
      setMappings(result.suggested_mappings.slice(0, 1).map((m) => ({ from: m.from, to: m.to })));
      setStage('review');
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not read the file'));
    } finally {
      setBusy(false);
    }
  }, []);

  const toggle = useCallback((key: string): void => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const setAll = useCallback(
    (on: boolean): void => setSelected(on && preview ? new Set(preview.playlists.map((p) => p.key)) : new Set<string>()),
    [preview]
  );

  const setMapping = useCallback((index: number, patch: Partial<ItunesPathMapping>): void => {
    setMappings((prev) => prev.map((m, i) => (i === index ? { ...m, ...patch } : m)));
  }, []);
  const addMapping = useCallback((): void => setMappings((prev) => [...prev, { from: '', to: '' }]), []);
  const removeMapping = useCallback(
    (index: number): void => setMappings((prev) => prev.filter((_, i) => i !== index)),
    []
  );

  const start = useCallback(async (): Promise<void> => {
    if (!preview) return;
    setBusy(true);
    setError(null);
    try {
      const all = selected.size === preview.playlists.length;
      await commitItunesImport(preview.import_id, {
        playlists: all ? 'all' : Array.from(selected),
        path_mappings: mappings.filter((m) => m.from.trim() && m.to.trim()),
        monitor_mode: monitorMode,
        name_prefix: namePrefix.trim() ? namePrefix : null,
        include_folders: includeFolders,
        import_play_stats: importPlayStats,
      });
      setStatus(null);
      setStage('running');
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not start the import'));
    } finally {
      setBusy(false);
    }
  }, [preview, selected, mappings, monitorMode, namePrefix, includeFolders, importPlayStats]);

  const poll = useCallback((): void => {
    if (!preview) return;
    getItunesImportStatus(preview.import_id)
      .then((s) => {
        setStatus(s);
        if (s.state === 'completed' || s.state === 'failed') setStage('done');
      })
      .catch((err: unknown) => setError(errorMessage(err, 'Could not read import progress')));
  }, [preview]);

  usePolling(poll, POLL_MS, stage === 'running');
  useEffect(() => {
    if (stage === 'running') poll();
  }, [stage, poll]);

  const reset = useCallback((): void => {
    setStage('upload');
    setPreview(null);
    setStatus(null);
    setError(null);
    setSelected(new Set<string>());
    setMappings([]);
  }, []);

  return {
    stage, busy, error, preview, selected, mappings, monitorMode, namePrefix, includeFolders, importPlayStats, status,
    upload, toggle, setAll, setMapping, addMapping, removeMapping, setMonitorMode, setNamePrefix, setIncludeFolders,
    setImportPlayStats, start, reset,
  };
}
