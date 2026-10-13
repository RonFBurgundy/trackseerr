import { useCallback, useRef, useState } from 'react';
import {
  relinkFile,
  type RenameRelinkFile,
  type RenameRelinkRequest,
} from '@/services/renameService';

const PREFER_QUALITY_KEY = 'trackseerr.rename.relinkPreferQuality';

function readPreferQuality(): boolean {
  try {
    return localStorage.getItem(PREFER_QUALITY_KEY) === '1';
  } catch {
    return false;
  }
}

function writePreferQuality(value: boolean): void {
  try {
    localStorage.setItem(PREFER_QUALITY_KEY, value ? '1' : '0');
  } catch {
    // Storage can be blocked (private window); the preference then lasts only for this session.
  }
}

export interface RelinkConflict {
  request: RenameRelinkRequest;
  existing: RenameRelinkFile;
  incoming: RenameRelinkFile;
}

export interface UseRenameRelinkOptions {
  onRelinked: () => void;
}

export interface UseRenameRelinkReturn {
  busyFileId: string | null;
  conflict: RelinkConflict | null;
  error: string | null;
  preferQuality: boolean;
  setPreferQuality: (value: boolean) => void;
  relink: (fileId: string, targetTrackId: string) => Promise<void>;
  resolve: (choice: 'keep_incoming' | 'keep_existing') => Promise<void>;
  dismissConflict: () => void;
}

export function useRenameRelink({ onRelinked }: UseRenameRelinkOptions): UseRenameRelinkReturn {
  const [busyFileId, setBusyFileId] = useState<string | null>(null);
  const [conflict, setConflict] = useState<RelinkConflict | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preferQuality, setPreferQualityState] = useState<boolean>(readPreferQuality);
  const onRelinkedRef = useRef<() => void>(onRelinked);
  onRelinkedRef.current = onRelinked;

  const setPreferQuality = useCallback((value: boolean): void => {
    setPreferQualityState(value);
    writePreferQuality(value);
  }, []);

  const send = useCallback(async (request: RenameRelinkRequest): Promise<void> => {
    setBusyFileId(request.file_id);
    setError(null);
    try {
      const outcome = await relinkFile(request);
      if (outcome.kind === 'ok') {
        setConflict(null);
        onRelinkedRef.current();
      } else if (outcome.kind === 'conflict') {
        setConflict({
          request,
          existing: outcome.data.existing,
          incoming: outcome.data.incoming,
        });
      } else {
        setConflict(null);
        setError(outcome.message);
      }
    } finally {
      setBusyFileId(null);
    }
  }, []);

  const relink = useCallback(
    async (fileId: string, targetTrackId: string): Promise<void> => {
      const request: RenameRelinkRequest = { file_id: fileId, target_track_id: targetTrackId };
      if (preferQuality) request.conflict = 'prefer_quality';
      await send(request);
    },
    [preferQuality, send]
  );

  const resolve = useCallback(
    async (choice: 'keep_incoming' | 'keep_existing'): Promise<void> => {
      if (!conflict) return;
      await send({ ...conflict.request, conflict: choice });
    },
    [conflict, send]
  );

  const dismissConflict = useCallback((): void => setConflict(null), []);

  return {
    busyFileId,
    conflict,
    error,
    preferQuality,
    setPreferQuality,
    relink,
    resolve,
    dismissConflict,
  };
}
