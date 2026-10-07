import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { errorMessage } from '@/services/apiClient';
import {
  commitManualImport,
  fingerprintManualImportFile,
  getManualImportAlbumTracks,
  scanManualImport,
} from '@/services/manualImportService';
import type {
  CandidateTrack,
  ManualImportItem,
  ManualImportRow,
  ManualImportScanItem,
  ManualImportScanRequest,
  ManualImportScope,
} from '@/types/manualImport';

export interface UseManualImportOptions {
  scope: ManualImportScope | null;
  isOpen: boolean;
  /** Called after at least one file was imported so the parent can refresh its data. */
  onImported: () => void;
}

export interface UseManualImportReturn {
  rows: ManualImportRow[];
  folderPath: string;
  setFolderPath: (value: string) => void;
  needsFolderInput: boolean;
  scanning: boolean;
  scanned: boolean;
  scanError: string | null;
  committing: boolean;
  commitError: string | null;
  downloadCleared: boolean;
  identifyingAll: boolean;
  selectedCount: number;
  /** Row keys whose chosen track is also chosen by another checked row. */
  duplicateKeys: ReadonlySet<string>;
  canCommit: boolean;
  scan: () => Promise<void>;
  toggleRow: (key: string) => void;
  selectAll: (checked: boolean) => void;
  selectTrack: (key: string, trackId: string | null) => void;
  changeAlbum: (key: string, albumId: string) => Promise<void>;
  identifyRow: (key: string) => Promise<void>;
  identifyAllUnmatched: () => Promise<void>;
  commit: () => Promise<void>;
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

function scopeKeyOf(scope: ManualImportScope | null): string {
  if (scope === null) return '';
  switch (scope.kind) {
    case 'folder':
      return 'folder';
    case 'download':
      return `download:${scope.downloadId}`;
    case 'album':
      return `album:${scope.albumId}`;
    case 'files':
      return `files:${scope.filePaths.join('|')}`;
  }
}

export const rowKey = (row: ManualImportRow): string => row.item.file_path;

function toRow(item: ManualImportScanItem): ManualImportRow {
  const ids = new Set(item.candidate_tracks.map((c) => c.id));
  const preferred = [item.suggested_track_id, item.matched_track_id].find((id): id is string => id != null && ids.has(id));
  const selectedTrackId = preferred ?? null;
  return {
    item,
    checked: item.match_strength !== 'none' && selectedTrackId !== null,
    selectedTrackId,
    candidates: item.candidate_tracks,
    identify: { phase: 'idle' },
    loadingTracks: false,
    result: null,
  };
}

function buildItem(row: ManualImportRow, track: CandidateTrack): ManualImportItem {
  return {
    source_path: row.item.file_path,
    artist_id: track.artist_id,
    artist_name: track.artist_name,
    album_id: track.album_id,
    album_title: track.album_title,
    track_id: track.id,
    track_title: track.title,
    track_number: track.track_number ?? row.item.tags.track_number ?? 1,
    disc_number: track.disc_number ?? row.item.tags.disc_number ?? 1,
    year: row.item.tags.year,
    write_tags: true,
  };
}

/** State machine for the Manual Import modal: scan, per-row track choice, fingerprinting, commit. */
export function useManualImport({ scope, isOpen, onImported }: UseManualImportOptions): UseManualImportReturn {
  const [rows, setRows] = useState<ManualImportRow[]>([]);
  const [folderPath, setFolderPath] = useState<string>('');
  const [scanning, setScanning] = useState<boolean>(false);
  const [scanned, setScanned] = useState<boolean>(false);
  const [scanError, setScanError] = useState<string | null>(null);
  const [committing, setCommitting] = useState<boolean>(false);
  const [commitError, setCommitError] = useState<string | null>(null);
  const [downloadCleared, setDownloadCleared] = useState<boolean>(false);
  const [identifyingAll, setIdentifyingAll] = useState<boolean>(false);

  const controllerRef = useRef<AbortController | null>(null);
  const rowsRef = useRef<ManualImportRow[]>([]);
  rowsRef.current = rows;
  const onImportedRef = useRef(onImported);
  onImportedRef.current = onImported;

  const needsFolderInput = scope !== null && (scope.kind === 'folder' || scope.kind === 'album');

  const patchRow = useCallback((key: string, patch: Partial<ManualImportRow>) => {
    setRows((prev) => prev.map((r) => (r.item.file_path === key ? { ...r, ...patch } : r)));
  }, []);

  const scan = useCallback(async (): Promise<void> => {
    if (!scope) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const body: ManualImportScanRequest = {};
    if (scope.kind === 'download') body.download_id = scope.downloadId;
    if (scope.kind === 'album') body.album_id = scope.albumId;
    if (scope.kind === 'files') body.file_paths = scope.filePaths;
    const folder = folderPath.trim();
    if ((scope.kind === 'folder' || scope.kind === 'album') && folder) body.folder_path = folder;
    setScanning(true);
    setScanError(null);
    setCommitError(null);
    try {
      const items = await scanManualImport(body, controller.signal);
      if (controller.signal.aborted) return;
      setRows(items.map(toRow));
      setScanned(true);
    } catch (err: unknown) {
      if (isAbort(err) || controller.signal.aborted) return;
      setScanError(errorMessage(err, 'Failed to scan for audio files'));
    } finally {
      if (!controller.signal.aborted) setScanning(false);
    }
  }, [scope, folderPath]);

  // Reset on open/scope change; download scope scans immediately. Closing aborts every in-flight request.
  const scopeKey = scopeKeyOf(scope);
  const scanRef = useRef(scan);
  scanRef.current = scan;
  useEffect(() => {
    if (!isOpen || scope === null) return undefined;
    setRows([]);
    setFolderPath('');
    setScanned(false);
    setScanError(null);
    setCommitError(null);
    setDownloadCleared(false);
    setScanning(false);
    setIdentifyingAll(false);
    if (scope.kind === 'download' || scope.kind === 'files') void scanRef.current();
    return () => {
      controllerRef.current?.abort();
      controllerRef.current = null;
    };
  }, [isOpen, scopeKey]);

  const toggleRow = useCallback((key: string) => {
    setRows((prev) => prev.map((r) => (r.item.file_path === key && r.result?.status !== 'imported' ? { ...r, checked: !r.checked } : r)));
  }, []);

  const selectAll = useCallback((checked: boolean) => {
    setRows((prev) =>
      prev.map((r) => (r.result?.status === 'imported' ? r : { ...r, checked: checked && r.selectedTrackId !== null }))
    );
  }, []);

  const selectTrack = useCallback((key: string, trackId: string | null) => {
    setRows((prev) =>
      prev.map((r) =>
        r.item.file_path === key
          ? { ...r, selectedTrackId: trackId, checked: trackId === null ? false : r.checked || r.selectedTrackId === null }
          : r
      )
    );
  }, []);

  const changeAlbum = useCallback(
    async (key: string, albumId: string): Promise<void> => {
      const signal = controllerRef.current?.signal;
      patchRow(key, { loadingTracks: true });
      try {
        const tracks = await getManualImportAlbumTracks(albumId, signal);
        if (signal?.aborted) return;
        patchRow(key, { candidates: tracks, selectedTrackId: null, checked: false, loadingTracks: false });
      } catch (err: unknown) {
        if (isAbort(err)) return;
        patchRow(key, {
          loadingTracks: false,
          identify: { phase: 'failed', message: errorMessage(err, 'Failed to load album tracks') },
        });
      }
    },
    [patchRow]
  );

  const identifyOne = useCallback(
    async (key: string, signal: AbortSignal): Promise<void> => {
      patchRow(key, { identify: { phase: 'running' } });
      try {
        const res = await fingerprintManualImportFile(key, signal);
        if (signal.aborted) return;
        if (!res.success) {
          patchRow(key, { identify: { phase: 'failed', message: res.message } });
          return;
        }
        const { fingerprint, library_track: lib } = res;
        const label = `${fingerprint.title ?? 'Unknown title'} — ${fingerprint.artist ?? 'Unknown artist'} · ${Math.round(fingerprint.score * 100)}%`;
        let candidates = rowsRef.current.find((r) => r.item.file_path === key)?.candidates ?? [];
        let selected: string | null = null;
        if (lib) {
          selected = candidates.some((c) => c.id === lib.id) ? lib.id : null;
          if (selected === null && lib.album_id) {
            const loaded = await getManualImportAlbumTracks(lib.album_id, signal);
            if (signal.aborted) return;
            if (loaded.some((c) => c.id === lib.id)) {
              candidates = loaded;
              selected = lib.id;
            }
          }
        }
        patchRow(key, {
          identify: { phase: 'done', message: label },
          candidates,
          ...(selected !== null ? { selectedTrackId: selected, checked: true } : {}),
        });
      } catch (err: unknown) {
        if (isAbort(err) || signal.aborted) return;
        patchRow(key, { identify: { phase: 'failed', message: errorMessage(err, 'Fingerprint lookup failed') } });
      }
    },
    [patchRow]
  );

  const identifyRow = useCallback(
    async (key: string): Promise<void> => {
      const signal = controllerRef.current?.signal ?? new AbortController().signal;
      await identifyOne(key, signal);
    },
    [identifyOne]
  );

  const identifyAllUnmatched = useCallback(async (): Promise<void> => {
    const controller = controllerRef.current ?? new AbortController();
    controllerRef.current = controller;
    const keys = rowsRef.current
      .filter((r) => r.result?.status !== 'imported' && (r.item.match_strength === 'none' || r.item.match_strength === 'weak'))
      .map((r) => r.item.file_path);
    setIdentifyingAll(true);
    try {
      for (const key of keys) {
        if (controller.signal.aborted) break;
        await identifyOne(key, controller.signal);
      }
    } finally {
      setIdentifyingAll(false);
    }
  }, [identifyOne]);

  const chosen = useMemo(
    () =>
      rows
        .filter((r) => r.checked && r.selectedTrackId !== null && r.result?.status !== 'imported')
        .map((r) => ({ row: r, track: r.candidates.find((c) => c.id === r.selectedTrackId) }))
        .filter((e): e is { row: ManualImportRow; track: CandidateTrack } => e.track !== undefined),
    [rows]
  );

  const duplicateKeys = useMemo<ReadonlySet<string>>(() => {
    const byTrack = new Map<string, string[]>();
    for (const { row, track } of chosen) {
      byTrack.set(track.id, [...(byTrack.get(track.id) ?? []), row.item.file_path]);
    }
    const dupes = new Set<string>();
    for (const keys of byTrack.values()) if (keys.length > 1) keys.forEach((k) => dupes.add(k));
    return dupes;
  }, [chosen]);

  const canCommit = chosen.length > 0 && duplicateKeys.size === 0 && !committing && !scanning;

  const commit = useCallback(async (): Promise<void> => {
    if (!scope || chosen.length === 0 || duplicateKeys.size > 0) return;
    setCommitting(true);
    setCommitError(null);
    try {
      const items = chosen.map(({ row, track }) => buildItem(row, track));
      const res = await commitManualImport(
        items,
        scope.kind === 'download' ? scope.downloadId : undefined,
        scope.kind === 'album' ? scope.issueId : undefined
      );
      const byPath = new Map(res.results.map((r) => [r.source_path ?? '', r]));
      setRows((prev) =>
        prev.map((r) => {
          const idx = items.findIndex((i) => i.source_path === r.item.file_path);
          if (idx === -1) return r;
          const result = byPath.get(r.item.file_path) ?? res.results[idx] ?? null;
          return result ? { ...r, result, checked: result.status === 'failed' } : r;
        })
      );
      setDownloadCleared(res.download_cleared);
      if (res.imported_count > 0) onImportedRef.current();
    } catch (err: unknown) {
      setCommitError(errorMessage(err, 'Import failed'));
    } finally {
      setCommitting(false);
    }
  }, [scope, chosen, duplicateKeys]);

  return {
    rows,
    folderPath,
    setFolderPath,
    needsFolderInput,
    scanning,
    scanned,
    scanError,
    committing,
    commitError,
    downloadCleared,
    identifyingAll,
    selectedCount: chosen.length,
    duplicateKeys,
    canCommit,
    scan,
    toggleRow,
    selectAll,
    selectTrack,
    changeAlbum,
    identifyRow,
    identifyAllUnmatched,
    commit,
  };
}
