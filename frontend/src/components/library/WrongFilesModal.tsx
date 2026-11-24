import React, { useState } from 'react';
import { Loader2 } from 'lucide-react';
import type { WrongFileItem } from '@/types/models';
import { useRenameRelink } from '@/hooks/useRenameRelink';
import { useWrongFiles } from '@/hooks/useWrongFiles';
import { ConfirmDialog, ObsidianModal, TapeDeckButton } from '@/components/ui';
import { RelinkConflictModal } from './RelinkConflictModal';

export interface WrongFilesModalProps {
  isOpen: boolean;
  onClose: () => void;
}

function formatScore(score: number): string {
  const pct = score <= 1 ? Math.round(score * 100) : Math.round(score);
  return `${pct}%`;
}

export const WrongFilesModal: React.FC<WrongFilesModalProps> = ({ isOpen, onClose }) => {
  const {
    items,
    total,
    checked,
    libraryFiles,
    isLoading,
    error,
    status,
    starting,
    checkError,
    busyFileId: deleteBusyId,
    actionError,
    startCheck,
    remove,
    deleteFile,
    reload,
  } = useWrongFiles(isOpen);

  const {
    busyFileId: relinkBusyId,
    conflict,
    error: relinkError,
    relink,
    resolve: resolveConflict,
    dismissConflict,
  } = useRenameRelink({
    onRelinked: (fileId) => {
      remove(fileId);
    },
  });

  const [pendingDelete, setPendingDelete] = useState<WrongFileItem | null>(null);

  const running = status?.running === true;
  const keyMissing = status?.api_key_set === false;
  const checkDisabled = running || starting || keyMissing;
  const inlineError = checkError || actionError || relinkError;

  const handleConfirmDelete = async (): Promise<void> => {
    if (!pendingDelete) return;
    const deleted = await deleteFile(pendingDelete.file_id);
    if (deleted) setPendingDelete(null);
  };

  return (
    <>
      <ObsidianModal
        isOpen={isOpen}
        onClose={onClose}
        title="Wrong files"
        subtitle="Files whose audio is a different song from the one they're filed as"
        maxWidth="sm:max-w-4xl"
      >
        <div className="flex flex-col gap-3 min-h-0 h-full">
          <div className="flex flex-col gap-1.5 shrink-0">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-col gap-0.5 text-xs font-mono text-neutral-300">
                {running ? (
                  <span>
                    Checking files&hellip; {status.done} of {status.total}
                  </span>
                ) : (
                  <>
                    <span>
                      Checked {checked} of {libraryFiles} files
                    </span>
                    {keyMissing && (
                      <span className="text-[#ffc94d]">
                        Add an AcoustID API key in Settings &rarr; Media Management to check files
                      </span>
                    )}
                  </>
                )}
              </div>
              <TapeDeckButton
                size="sm"
                variant="amber"
                disabled={checkDisabled}
                onClick={() => void startCheck()}
                icon={running || starting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
              >
                Check all files
              </TapeDeckButton>
            </div>
            <p className="text-xs text-neutral-400 font-mono">
              The first check fingerprints every file and can take about an hour for a large library. Later checks only look at new or changed files.
            </p>
          </div>

          {inlineError && <p className="text-xs text-[var(--status-error)] shrink-0">{inlineError}</p>}

          <div className="min-w-0 min-h-0 overflow-y-auto flex-1 sm:flex-none sm:max-h-[60dvh] flex flex-col gap-3">
            {isLoading && (
              <div className="flex items-center gap-2 text-xs text-neutral-400 py-4">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading&hellip;
              </div>
            )}
            {error && (
              <div className="flex flex-col gap-2 py-2">
                <p className="text-xs text-[var(--status-error)]">{error}</p>
                <div>
                  <TapeDeckButton size="sm" onClick={reload}>
                    Try again
                  </TapeDeckButton>
                </div>
              </div>
            )}
            {!isLoading && !error && total === 0 && !running && (
              <p className="text-xs text-neutral-400 py-4">No wrong files found.</p>
            )}
            {items.map((item) => {
              const rowBusy = relinkBusyId === item.file_id || deleteBusyId === item.file_id;
              const meta = [item.artist_name, item.album_title].filter(Boolean).join(' — ');
              const actualTitle = item.actual_title || 'Unknown track';
              const actualArtist = item.actual_artist || 'Unknown artist';
              const scoreText = item.score != null ? ` (${formatScore(item.score)})` : '';
              return (
                <div
                  key={item.file_id}
                  className="border border-[#2a2a2a] bg-[#121212] rounded-[4px] p-2.5 flex flex-col gap-2 min-w-0"
                >
                  <div className="flex flex-col gap-0.5 min-w-0">
                    {meta && <span className="text-xs text-neutral-400 block truncate">{meta}</span>}
                    <span className="text-white font-semibold block truncate">{item.track_title}</span>
                    <span className="text-xs text-[#ffc94d] block truncate">
                      Audio is: {actualTitle} by {actualArtist}
                      {scoreText}
                    </span>
                    {item.relative_path && (
                      <span className="text-[11px] text-neutral-500 font-mono block truncate" title={item.relative_path}>
                        {item.relative_path}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5 flex-wrap">
                    {item.actual_track_id && (
                      <TapeDeckButton
                        size="sm"
                        variant="amber"
                        disabled={rowBusy}
                        onClick={() => void relink(item.file_id, item.actual_track_id!)}
                        icon={relinkBusyId === item.file_id ? <Loader2 className="h-3 w-3 animate-spin" /> : undefined}
                      >
                        Move to {item.actual_track_title || actualTitle}
                      </TapeDeckButton>
                    )}
                    <TapeDeckButton
                      size="sm"
                      disabled={rowBusy}
                      onClick={() => setPendingDelete(item)}
                      icon={deleteBusyId === item.file_id ? <Loader2 className="h-3 w-3 animate-spin" /> : undefined}
                    >
                      Delete file
                    </TapeDeckButton>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </ObsidianModal>

      <RelinkConflictModal
        conflict={conflict}
        busy={relinkBusyId !== null}
        onCancel={dismissConflict}
        onKeepExisting={() => void resolveConflict('keep_existing')}
        onKeepIncoming={() => void resolveConflict('keep_incoming')}
      />

      <ConfirmDialog
        isOpen={pendingDelete !== null}
        title="Delete file?"
        confirmLabel="Delete file"
        onConfirm={() => void handleConfirmDelete()}
        onCancel={() => {
          if (deleteBusyId === null) setPendingDelete(null);
        }}
        busy={pendingDelete !== null && deleteBusyId === pendingDelete.file_id}
      >
        <p className="text-xs sm:text-sm font-mono text-neutral-300">
          Move &ldquo;{pendingDelete?.relative_path || pendingDelete?.track_title}&rdquo; to the recycle bin?
        </p>
      </ConfirmDialog>
    </>
  );
};
