import React, { useMemo, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { Loader2 } from 'lucide-react';
import type { WrongFileItem } from '@/types/models';
import { relinkRenameBatch, type RenameRelinkBatchItem } from '@/services/renameService';
import { errorMessage } from '@/services/apiClient';
import { useRenameRelink } from '@/hooks/useRenameRelink';
import { useWrongFiles } from '@/hooks/useWrongFiles';
import {
  ConfirmDialog,
  ObsidianModal,
  SelectionCheckbox,
  TactileSwitch,
  TapeDeckButton,
} from '@/components/ui';
import { RelinkConflictModal } from './RelinkConflictModal';

export interface WrongFilesModalProps {
  isOpen: boolean;
  onClose: () => void;
}

function formatScore(score: number): string {
  const pct = score <= 1 ? Math.round(score * 100) : Math.round(score);
  return `${pct}%`;
}

function formatRemainingEstimate(
  done: number,
  total: number,
  startedAt: number | null | undefined,
  nowSeconds: number = Date.now() / 1000,
): string {
  if (done < 50 || startedAt == null || startedAt <= 0) {
    return '';
  }
  const elapsed = Math.max(1, nowSeconds - startedAt);
  const rate = done / elapsed;
  if (rate <= 0) {
    return '';
  }
  const remaining = Math.max(0, total - done);
  const totalMinutes = Math.round(remaining / rate / 60);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return ` \u2014 about ${hours} h ${minutes} min left`;
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
    selectedIds,
    toggleSelect,
    selectAll,
    selectNone,
    startCheck,
    remove,
    deleteFile,
    deleteFiles,
    reload,
  } = useWrongFiles(isOpen);

  const {
    busyFileId: relinkBusyId,
    conflict,
    error: relinkError,
    preferQuality,
    setPreferQuality,
    relink,
    resolve: resolveConflict,
    dismissConflict,
  } = useRenameRelink({
    onRelinked: (fileId) => {
      remove(fileId);
    },
  });

  const [pendingDelete, setPendingDelete] = useState<WrongFileItem | null>(null);
  const [pendingBatchDelete, setPendingBatchDelete] = useState<boolean>(false);
  const [batchDeleting, setBatchDeleting] = useState<boolean>(false);
  const [batchDeleteSummary, setBatchDeleteSummary] = useState<{
    deleted: number;
    failedLabels: string[];
  } | null>(null);

  const [batchMoving, setBatchMoving] = useState<boolean>(false);
  const [batchMoveError, setBatchMoveError] = useState<string | null>(null);
  const [batchMoveSummary, setBatchMoveSummary] = useState<{
    moved: number;
    conflictsSkipped: number;
    errorMessages: string[];
  } | null>(null);

  const parentRef = useRef<HTMLDivElement | null>(null);

  const rowVirtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 104,
    overscan: 8,
  });

  const selectedItems = useMemo(
    () => items.filter((item) => selectedIds.has(item.file_id)),
    [items, selectedIds]
  );
  const selectedCount = selectedItems.length;
  const movableSelectedItems = useMemo(
    () => selectedItems.filter((item) => Boolean(item.actual_track_id)),
    [selectedItems]
  );
  const movableCount = movableSelectedItems.length;
  const allSelected = items.length > 0 && selectedCount === items.length;

  const running = status?.running === true;
  const keyMissing = status?.api_key_set === false;
  const checkDisabled = running || starting || keyMissing;
  const inlineError = checkError || actionError || relinkError || batchMoveError;

  const handleConfirmDelete = async (): Promise<void> => {
    if (pendingBatchDelete) {
      const ids = selectedItems.map((item) => item.file_id);
      if (ids.length === 0) {
        setPendingBatchDelete(false);
        return;
      }
      const labelById = new Map(
        selectedItems.map((item) => [item.file_id, item.relative_path || item.track_title])
      );
      setBatchDeleting(true);
      setBatchDeleteSummary(null);
      setBatchMoveSummary(null);
      setBatchMoveError(null);
      try {
        const { deleted, failed } = await deleteFiles(ids);
        setBatchDeleteSummary({
          deleted,
          failedLabels: failed.map((id) => labelById.get(id) ?? id),
        });
        setPendingBatchDelete(false);
      } finally {
        setBatchDeleting(false);
      }
      return;
    }

    if (!pendingDelete) return;
    const deleted = await deleteFile(pendingDelete.file_id);
    if (deleted) setPendingDelete(null);
  };

  const handleBatchMove = async (): Promise<void> => {
    const batchItems: RenameRelinkBatchItem[] = [];
    for (const item of movableSelectedItems) {
      if (item.actual_track_id) {
        batchItems.push({
          file_id: item.file_id,
          target_track_id: item.actual_track_id,
        });
      }
    }
    if (batchItems.length === 0 || batchMoving || batchDeleting) return;

    setBatchMoving(true);
    setBatchMoveError(null);
    setBatchMoveSummary(null);
    setBatchDeleteSummary(null);

    const conflictChoice: 'prefer_quality' | 'skip' = preferQuality ? 'prefer_quality' : 'skip';
    const BATCH_SIZE = 500;
    const chunks: RenameRelinkBatchItem[][] = [];
    for (let i = 0; i < batchItems.length; i += BATCH_SIZE) {
      chunks.push(batchItems.slice(i, i + BATCH_SIZE));
    }

    let moved = 0;
    let conflictsSkipped = 0;
    const errorMessages: string[] = [];
    const removedIds = new Set<string>();

    try {
      for (const chunk of chunks) {
        const res = await relinkRenameBatch({
          items: chunk,
          conflict: conflictChoice,
        });
        moved += res.relinked;
        conflictsSkipped += res.conflicts_skipped;

        for (const r of res.results) {
          if (r.status === 'relinked' && r.file_id) {
            removedIds.add(r.file_id);
          }
          if (r.recycled_file_id) {
            removedIds.add(r.recycled_file_id);
          }
          if (r.status === 'error' && r.message) {
            errorMessages.push(r.message);
          }
        }
      }

      for (const id of removedIds) {
        remove(id);
      }

      setBatchMoveSummary({
        moved,
        conflictsSkipped,
        errorMessages,
      });
    } catch (err: unknown) {
      setBatchMoveError(errorMessage(err, 'Failed to move selected files'));
    } finally {
      setBatchMoving(false);
    }
  };

  const footer = (
    <>
      <TapeDeckButton onClick={onClose}>Close</TapeDeckButton>
      <TapeDeckButton
        disabled={selectedCount === 0 || batchMoving || batchDeleting}
        onClick={() => setPendingBatchDelete(true)}
        icon={batchDeleting ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
      >
        {batchDeleting ? 'Deleting...' : `Delete selected (${selectedCount})`}
      </TapeDeckButton>
      <TapeDeckButton
        variant="amber"
        disabled={movableCount === 0 || batchMoving || batchDeleting}
        onClick={() => void handleBatchMove()}
        icon={batchMoving ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
      >
        {batchMoving ? 'Moving...' : `Move selected (${movableCount})`}
      </TapeDeckButton>
    </>
  );

  const confirmBusy = pendingBatchDelete
    ? batchDeleting
    : pendingDelete !== null && deleteBusyId === pendingDelete.file_id;

  return (
    <>
      <ObsidianModal
        isOpen={isOpen}
        onClose={onClose}
        title="Wrong files"
        subtitle="Files whose audio is a different song from the one they're filed as"
        maxWidth="sm:max-w-4xl"
        footer={footer}
      >
        <div className="flex flex-col gap-3 min-h-0 h-full">
          <div className="flex flex-col gap-1.5 shrink-0">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-col gap-0.5 text-xs font-mono text-neutral-300">
                {running ? (
                  <span>
                    Checking files&hellip; {status.done} of {status.total}
                    {formatRemainingEstimate(status.done, status.total, status.started_at)}
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
              The first check listens to every file, about 100 a minute, so a large library takes several hours. You can close this window while it runs. Later checks only look at new or changed files.
            </p>
          </div>

          {items.length > 0 && (
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#222222] pb-2 text-xs font-mono shrink-0">
              <div className="flex items-center gap-2">
                <SelectionCheckbox
                  name="wrong-files-select-all"
                  checked={allSelected}
                  onChange={() => {
                    if (allSelected) selectNone();
                    else selectAll();
                  }}
                  label="Select all"
                  inline
                />
                <span className="text-neutral-300">Select all</span>
                <span className="text-neutral-500">•</span>
                <button
                  type="button"
                  onClick={selectNone}
                  className="text-neutral-400 hover:text-white transition-colors"
                >
                  None
                </button>
              </div>
              <div className="flex flex-wrap items-center gap-2 text-neutral-400">
                <TactileSwitch
                  name="wrong-files-prefer-quality"
                  label="Keep the higher-quality file when the right song already has one"
                  checked={preferQuality}
                  onChange={setPreferQuality}
                  className="max-w-[18rem] text-[11px] leading-tight"
                />
                <span>
                  <strong className="text-white">{selectedCount}</strong> of {items.length} selected
                </span>
              </div>
            </div>
          )}

          {inlineError && <p className="text-xs text-[var(--status-error)] shrink-0">{inlineError}</p>}

          {batchMoveSummary && (
            <div
              role="status"
              className="p-2.5 rounded-[3px] border border-[#2a2a2a] bg-[#161616] text-xs font-mono text-neutral-200 space-y-1 shrink-0"
            >
              <p>
                Moved {batchMoveSummary.moved}. {batchMoveSummary.conflictsSkipped} skipped because the right song already has a file.
              </p>
              {batchMoveSummary.errorMessages.length > 0 && (
                <ul className="list-disc pl-4 text-[var(--status-error)] space-y-0.5">
                  {batchMoveSummary.errorMessages.map((msg, idx) => (
                    <li key={idx}>{msg}</li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {batchDeleteSummary && (
            <div
              role="status"
              className="p-2.5 rounded-[3px] border border-[#2a2a2a] bg-[#161616] text-xs font-mono text-neutral-200 space-y-1 shrink-0"
            >
              <p>Deleted {batchDeleteSummary.deleted}.</p>
              {batchDeleteSummary.failedLabels.length > 0 && (
                <ul className="list-disc pl-4 text-[var(--status-error)] space-y-0.5">
                  {batchDeleteSummary.failedLabels.map((label, idx) => (
                    <li key={idx} className="truncate">
                      {label}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {isLoading && (
            <div className="flex items-center gap-2 text-xs text-neutral-400 py-4 shrink-0">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading&hellip;
            </div>
          )}

          {error && (
            <div className="flex flex-col gap-2 py-2 shrink-0">
              <p className="text-xs text-[var(--status-error)]">{error}</p>
              <div>
                <TapeDeckButton size="sm" onClick={reload}>
                  Try again
                </TapeDeckButton>
              </div>
            </div>
          )}

          {!isLoading && !error && total === 0 && !running && (
            <p className="text-xs text-neutral-400 py-4 shrink-0">No wrong files found.</p>
          )}

          {items.length > 0 && (
            <div
              ref={parentRef}
              tabIndex={0}
              role="region"
              aria-label="Wrong files (scrollable)"
              className="virtual-scroll flex-1 min-h-0 overflow-y-auto border border-[#222222] rounded-[4px] bg-[#0d0d0d] divide-y divide-[#1c1c1c]"
              style={{ maxHeight: 'min(500px, 60vh)' }}
            >
              <div
                style={{
                  height: `${rowVirtualizer.getTotalSize()}px`,
                  width: '100%',
                  position: 'relative',
                }}
              >
                {rowVirtualizer.getVirtualItems().map((virtualRow) => {
                  const item = items[virtualRow.index];
                  if (!item) return null;
                  const isSelected = selectedIds.has(item.file_id);
                  const rowBusy = relinkBusyId === item.file_id || deleteBusyId === item.file_id;
                  const meta = [item.artist_name, item.album_title].filter(Boolean).join(' — ');
                  const actualTitle = item.actual_title || 'Unknown track';
                  const actualArtist = item.actual_artist || 'Unknown artist';
                  const scoreText = item.score != null ? ` (${formatScore(item.score)})` : '';
                  const actualTrackId = item.actual_track_id ?? null;
                  const moveTarget = [
                    item.actual_artist_name || actualArtist,
                    item.actual_track_title || actualTitle,
                  ]
                    .filter(Boolean)
                    .join(' — ');

                  return (
                    <div
                      key={item.file_id}
                      data-index={virtualRow.index}
                      ref={rowVirtualizer.measureElement}
                      style={{
                        position: 'absolute',
                        top: 0,
                        left: 0,
                        width: '100%',
                        transform: `translateY(${virtualRow.start}px)`,
                      }}
                      className={`p-2.5 transition-colors flex items-start gap-2.5 min-w-0 ${
                        isSelected ? 'bg-[#181818]' : 'hover:bg-[#141414]'
                      }`}
                    >
                      <div className="pt-0.5 shrink-0">
                        <SelectionCheckbox
                          name={`wrong-file-${item.file_id}`}
                          checked={isSelected}
                          onChange={() => toggleSelect(item.file_id)}
                          label={`Select ${item.track_title}`}
                          inline
                        />
                      </div>

                      <div className="min-w-0 flex-1 space-y-1 font-mono text-[11px] leading-tight">
                        {meta && <div className="min-w-0 truncate text-neutral-400">{meta}</div>}
                        <div className="min-w-0 truncate font-semibold text-neutral-100">
                          {item.track_title}
                        </div>
                        <div className="min-w-0 truncate text-[#ffc94d]">
                          Audio is: {actualTitle} by {actualArtist}
                          {scoreText}
                        </div>
                        {actualTrackId && (
                          <div className="min-w-0 truncate text-neutral-400">
                            Moves to: {moveTarget}
                          </div>
                        )}
                        {item.relative_path && (
                          <div
                            tabIndex={0}
                            role="group"
                            aria-label="File path (scrolls horizontally)"
                            className="min-w-0 overflow-x-auto pb-1"
                          >
                            <div
                              className="w-max min-w-full whitespace-nowrap text-neutral-500"
                              title={item.relative_path}
                            >
                              {item.relative_path}
                            </div>
                          </div>
                        )}
                        <div className="flex gap-1.5 shrink-0 pt-1">
                          {actualTrackId && (
                            <TapeDeckButton
                              size="sm"
                              variant="amber"
                              disabled={rowBusy || batchMoving || batchDeleting}
                              onClick={() => void relink(item.file_id, actualTrackId)}
                              icon={
                                relinkBusyId === item.file_id ? (
                                  <Loader2 className="h-3 w-3 animate-spin" />
                                ) : undefined
                              }
                            >
                              Move
                            </TapeDeckButton>
                          )}
                          <TapeDeckButton
                            size="sm"
                            disabled={rowBusy || batchMoving || batchDeleting}
                            onClick={() => setPendingDelete(item)}
                            icon={
                              deleteBusyId === item.file_id ? (
                                <Loader2 className="h-3 w-3 animate-spin" />
                              ) : undefined
                            }
                          >
                            Delete
                          </TapeDeckButton>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
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
        isOpen={pendingDelete !== null || pendingBatchDelete}
        title={pendingBatchDelete ? 'Delete selected files?' : 'Delete file?'}
        confirmLabel={pendingBatchDelete ? 'Delete selected' : 'Delete file'}
        onConfirm={() => void handleConfirmDelete()}
        onCancel={() => {
          if (!confirmBusy) {
            setPendingDelete(null);
            setPendingBatchDelete(false);
          }
        }}
        busy={confirmBusy}
      >
        <p className="text-xs sm:text-sm font-mono text-neutral-300">
          {pendingBatchDelete
            ? `Move ${selectedCount} files to the recycle bin?`
            : `Move “${pendingDelete?.relative_path || pendingDelete?.track_title}” to the recycle bin?`}
        </p>
      </ConfirmDialog>
    </>
  );
};
