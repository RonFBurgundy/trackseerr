import React, { useMemo, useRef, useState } from 'react';
import {
  AudioLines,
  ChevronDown,
  FileCheck,
  Link2,
  Loader2,
  RefreshCw,
  Trash2,
  TriangleAlert,
} from 'lucide-react';
import { useVirtualizer } from '@tanstack/react-virtual';
import type { WrongFileItem } from '@/types/models';
import { relinkRenameBatch, type RenameRelinkBatchItem } from '@/services/renameService';
import { errorMessage } from '@/services/apiClient';
import { useRenameRelink } from '@/hooks/useRenameRelink';
import { useWrongFiles, type WrongFileKind } from '@/hooks/useWrongFiles';
import {
  ConfirmDialog,
  ObsidianModal,
  SelectionCheckbox,
  TactileSwitch,
  TapeDeckButton,
} from '@/components/ui';
import { CHIP_BASE, CHIP_RISKY, CHIP_SAFE } from './libraryChips';
import { RelinkConflictModal } from './RelinkConflictModal';

export interface WrongFilesModalProps {
  isOpen: boolean;
  onClose: () => void;
}

interface KindMeta {
  label: string;
  risky: boolean;
  help: string;
}

const KIND_META: Record<WrongFileKind, KindMeta> = {
  different_song: {
    label: 'Different song',
    risky: false,
    help: "The audio is a different song from the one it's filed as",
  },
  different_version: {
    label: 'Different version',
    risky: true,
    help: "Same song, another version — a remix, remaster or live take. AcoustID often can't tell versions apart, so check these one by one",
  },
};

const KIND_ORDER: ReadonlyArray<WrongFileKind> = ['different_song', 'different_version'];

const KindChip: React.FC<{ kind: WrongFileKind }> = ({ kind }) => {
  const info = KIND_META[kind];
  return (
    <span title={info.help} className={`${CHIP_BASE} ${info.risky ? CHIP_RISKY : CHIP_SAFE}`}>
      {info.label}
    </span>
  );
};

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
  return ` — about ${hours} h ${minutes} min left`;
}

export const WrongFilesModal: React.FC<WrongFilesModalProps> = ({ isOpen, onClose }) => {
  const {
    items,
    filteredItems,
    kindCounts,
    activeKinds,
    toggleKind,
    clearKinds,
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
    selectSafe,
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
    onRelinked: (fileId, res) => {
      remove(fileId);
      if (res.recycled_file_id) remove(res.recycled_file_id);
    },
  });

  const [pendingDelete, setPendingDelete] = useState<WrongFileItem | null>(null);
  const [pendingBatchDelete, setPendingBatchDelete] = useState<boolean>(false);
  const [batchDeleting, setBatchDeleting] = useState<boolean>(false);
  const [noteExpanded, setNoteExpanded] = useState<boolean>(false);
  const [batchDeleteSummary, setBatchDeleteSummary] = useState<{
    deleted: number;
    failedLabels: string[];
  } | null>(null);

  const [batchRelinking, setBatchRelinking] = useState<boolean>(false);
  const [batchRelinkError, setBatchRelinkError] = useState<string | null>(null);
  const [batchRelinkSummary, setBatchRelinkSummary] = useState<{
    relinked: number;
    conflictsSkipped: number;
    errors: number;
    errorMessages: string[];
  } | null>(null);

  const parentRef = useRef<HTMLDivElement | null>(null);

  const rowVirtualizer = useVirtualizer({
    count: filteredItems.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 92,
    overscan: 8,
  });

  const selectedItems = useMemo(
    () => items.filter((item) => selectedIds.has(item.file_id)),
    [items, selectedIds]
  );
  const selectedCount = selectedItems.length;
  const relinkableSelectedItems = useMemo(
    () => selectedItems.filter((item) => Boolean(item.actual_track_id)),
    [selectedItems]
  );
  const relinkableCount = relinkableSelectedItems.length;
  const selectedInView = filteredItems.reduce(
    (n, item) => (selectedIds.has(item.file_id) ? n + 1 : n),
    0
  );
  const allSelected = filteredItems.length > 0 && selectedInView === filteredItems.length;

  const checkRunning = status?.running === true;
  const keyMissing = status?.api_key_set === false;
  const checkDisabled = checkRunning || starting || keyMissing;
  const checkTitle = keyMissing
    ? 'Set an AcoustID API key in Settings → Media Management'
    : 'Fingerprint library audio and compare it with the linked track';

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
      setBatchRelinkSummary(null);
      setBatchRelinkError(null);
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

  const handleBatchRelink = async (): Promise<void> => {
    const batchItems: RenameRelinkBatchItem[] = [];
    for (const item of relinkableSelectedItems) {
      if (item.actual_track_id) {
        batchItems.push({
          file_id: item.file_id,
          target_track_id: item.actual_track_id,
        });
      }
    }
    if (batchItems.length === 0 || batchRelinking || batchDeleting) return;

    setBatchRelinking(true);
    setBatchRelinkError(null);
    setBatchRelinkSummary(null);
    setBatchDeleteSummary(null);

    const conflictChoice: 'prefer_quality' | 'skip' = preferQuality ? 'prefer_quality' : 'skip';
    const BATCH_SIZE = 500;
    const chunks: RenameRelinkBatchItem[][] = [];
    for (let i = 0; i < batchItems.length; i += BATCH_SIZE) {
      chunks.push(batchItems.slice(i, i + BATCH_SIZE));
    }

    let relinked = 0;
    let conflictsSkipped = 0;
    let errors = 0;
    const errorMessages: string[] = [];
    const removedIds = new Set<string>();

    try {
      for (const chunk of chunks) {
        const res = await relinkRenameBatch({
          items: chunk,
          conflict: conflictChoice,
        });
        relinked += res.relinked;
        conflictsSkipped += res.conflicts_skipped;
        errors += res.errors;

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

      setBatchRelinkSummary({
        relinked,
        conflictsSkipped,
        errors,
        errorMessages,
      });
    } catch (err: unknown) {
      setBatchRelinkError(errorMessage(err, 'Failed to relink selected files'));
    } finally {
      setBatchRelinking(false);
    }
  };

  const handleReload = (): void => {
    setBatchRelinkSummary(null);
    setBatchRelinkError(null);
    setBatchDeleteSummary(null);
    reload();
  };

  const handleClose = (): void => {
    setBatchRelinkSummary(null);
    setBatchRelinkError(null);
    setBatchDeleteSummary(null);
    onClose();
  };

  const footer = (
    <>
      <TapeDeckButton onClick={handleClose}>
        {batchRelinkSummary || batchDeleteSummary ? 'Done' : 'Cancel'}
      </TapeDeckButton>
      <TapeDeckButton
        disabled={selectedCount === 0 || batchRelinking || batchDeleting}
        onClick={() => setPendingBatchDelete(true)}
        icon={batchDeleting ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
      >
        {batchDeleting ? 'Deleting...' : `Delete selected (${selectedCount})`}
      </TapeDeckButton>
      <TapeDeckButton
        variant="amber"
        disabled={relinkableCount === 0 || batchRelinking || batchDeleting}
        onClick={() => void handleBatchRelink()}
        icon={batchRelinking ? <Loader2 className="h-4 w-4 animate-spin" /> : <Link2 className="h-4 w-4" />}
      >
        {batchRelinking ? 'Relinking...' : `Relink selected (${relinkableCount})`}
      </TapeDeckButton>
    </>
  );

  const confirmBusy = pendingBatchDelete
    ? batchDeleting
    : pendingDelete !== null && deleteBusyId === pendingDelete.file_id;

  const statusAndErrors = (
    <>
      {checkRunning && status && (
        <div className="space-y-1 text-xs font-mono text-neutral-300" aria-live="polite">
          <p>
            Checking audio… {status.done}/{status.total}
            {formatRemainingEstimate(status.done, status.total, status.started_at)}
          </p>
          <div className="h-1 w-full overflow-hidden rounded-[2px] bg-[#1c1c1c]">
            <div
              role="progressbar"
              aria-label="Audio check progress"
              aria-valuemin={0}
              aria-valuenow={status.done}
              aria-valuemax={status.total}
              className="h-full bg-[#e5a00d] transition-[width]"
              style={{
                width: `${status.total > 0 ? Math.min(100, (status.done / status.total) * 100) : 0}%`,
              }}
            />
          </div>
        </div>
      )}
      {!checkRunning && status && status.finished_at != null && (
        <div className="space-y-0.5 text-xs font-mono text-neutral-400">
          <p>
            {status.verified} verified · {status.mismatched} mismatched ·{' '}
            {status.unknown} unknown · {status.errors} errors
          </p>
          {status.message && <p className="text-[#ffc94d]">{status.message}</p>}
        </div>
      )}
      {checkError && (
        <p role="alert" className="text-xs font-mono text-[var(--status-error)]">
          {checkError}
        </p>
      )}
      {actionError && (
        <p role="alert" className="text-xs font-mono text-[var(--status-error)]">
          {actionError}
        </p>
      )}
      {relinkError && (
        <p role="alert" className="text-xs font-mono text-[var(--status-error)]">
          {relinkError}
        </p>
      )}
      {batchRelinkError && (
        <p role="alert" className="text-xs font-mono text-[var(--status-error)]">
          {batchRelinkError}
        </p>
      )}
      {batchRelinkSummary && (
        <div className="space-y-1 text-xs font-mono text-neutral-300">
          <p>
            Relinked {batchRelinkSummary.relinked} · skipped {batchRelinkSummary.conflictsSkipped}{' '}
            (right track already has a file) · {batchRelinkSummary.errors} errors
          </p>
          {batchRelinkSummary.errorMessages.length > 0 && (
            <ul className="text-[var(--status-error)] space-y-0.5 max-h-32 overflow-y-auto pl-4 list-disc">
              {batchRelinkSummary.errorMessages.map((msg, i) => (
                <li key={i} className="break-all">
                  {msg}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {batchDeleteSummary && (
        <div className="space-y-1 text-xs font-mono text-neutral-300">
          <p>Deleted {batchDeleteSummary.deleted}.</p>
          {batchDeleteSummary.failedLabels.length > 0 && (
            <ul className="text-[var(--status-error)] space-y-0.5 max-h-32 overflow-y-auto pl-4 list-disc">
              {batchDeleteSummary.failedLabels.map((label, i) => (
                <li key={i} className="break-all">
                  {label}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );

  return (
    <>
      <ObsidianModal
        isOpen={isOpen}
        onClose={handleClose}
        title="Wrong files"
        subtitle="Files whose audio is a different song from the one they're filed as"
        maxWidth="sm:max-w-4xl"
        footer={footer}
      >
        <div className="space-y-4">
          {isLoading && (
            <div className="flex flex-col items-center justify-center gap-2 py-14 text-xs font-mono uppercase tracking-widest text-[var(--text-secondary)]">
              <Loader2 className="h-6 w-6 animate-spin text-[var(--accent-amber)]" />
              <span>Loading wrong files…</span>
            </div>
          )}

          {error && (
            <div
              role="alert"
              className="p-3 rounded-[3px] border border-[var(--status-error)]/50 bg-[var(--status-error)]/10 text-xs font-mono text-[var(--status-error)]"
            >
              {error}
            </div>
          )}

          {!isLoading && (
            <>
              {items.length === 0 ? (
                <div className="space-y-4">
                  <div className="py-12 text-center space-y-2">
                    <FileCheck className="h-8 w-8 text-[var(--accent-amber)] mx-auto opacity-70" />
                    <p className="text-sm font-medium text-white">No wrong files found</p>
                    <p className="text-xs text-neutral-400 font-mono">
                      Checked {checked} of {libraryFiles} files
                    </p>
                    {keyMissing && (
                      <p className="text-xs font-mono text-[#ffc94d]">
                        Set an AcoustID API key in Settings → Media Management
                      </p>
                    )}
                    <div className="pt-2 flex flex-wrap items-center justify-center gap-2">
                      <TapeDeckButton
                        size="sm"
                        disabled={checkDisabled}
                        title={checkTitle}
                        onClick={() => void startCheck()}
                        icon={
                          checkRunning || starting ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : (
                            <AudioLines className="h-3 w-3" />
                          )
                        }
                      >
                        Check all files
                      </TapeDeckButton>
                      <TapeDeckButton
                        size="sm"
                        onClick={handleReload}
                        icon={<RefreshCw className="h-3 w-3" />}
                        title="Reload wrong files"
                      >
                        Refresh
                      </TapeDeckButton>
                      <TapeDeckButton
                        size="sm"
                        onClick={() => setNoteExpanded((prev) => !prev)}
                        aria-expanded={noteExpanded}
                        icon={
                          <ChevronDown
                            className={`h-3 w-3 transition-transform ${noteExpanded ? 'rotate-180' : ''}`}
                          />
                        }
                      >
                        How long does this take?
                      </TapeDeckButton>
                    </div>
                    {noteExpanded && (
                      <p className="text-xs text-neutral-400 font-mono max-w-xl mx-auto pt-1">
                        The first check listens to every file, about 100 a minute, so a large library takes several hours. You can close this window while it runs. Later checks only look at new or changed files.
                      </p>
                    )}
                  </div>
                  {statusAndErrors}
                </div>
              ) : (
                <div className="space-y-3">
                  <p className="text-xs font-mono text-neutral-400">
                    Showing <strong className="text-white">{filteredItems.length}</strong> of{' '}
                    <strong className="text-white">{total}</strong> wrong files
                  </p>

                  {/* Kind filter bar */}
                  <div
                    role="group"
                    aria-label="Filter by kind"
                    className="tab-strip overflow-x-auto pb-0.5"
                  >
                    {KIND_ORDER.map((kind) => {
                      const info = KIND_META[kind];
                      const active = activeKinds.has(kind);
                      return (
                        <button
                          key={kind}
                          type="button"
                          aria-pressed={active}
                          title={info.help}
                          onClick={() => toggleKind(kind)}
                          className={`${CHIP_BASE} cursor-pointer transition-colors ${
                            active
                              ? 'border-[#e5a00d] bg-[#e5a00d] text-black font-bold'
                              : info.risky
                                ? `${CHIP_RISKY} hover:border-[#e5a00d]`
                                : `${CHIP_SAFE} hover:text-white hover:border-[#3a3a3a]`
                          }`}
                        >
                          {info.label} · {kindCounts[kind]}
                        </button>
                      );
                    })}
                    {activeKinds.size > 0 && (
                      <button
                        type="button"
                        onClick={clearKinds}
                        className={`${CHIP_BASE} cursor-pointer border-transparent text-neutral-400 hover:text-white underline`}
                      >
                        Clear filter
                      </button>
                    )}
                  </div>

                  {/* Header Toolbar */}
                  <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#222222] pb-2 text-xs font-mono">
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
                        onClick={selectSafe}
                        title="Select only different-song files with a known library track"
                        className="text-neutral-400 hover:text-white transition-colors"
                      >
                        Select safe only
                      </button>
                      <span className="text-neutral-500">•</span>
                      <button
                        type="button"
                        onClick={selectAll}
                        className="text-neutral-400 hover:text-white transition-colors"
                      >
                        All
                      </button>
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
                        name="wrong-files-relink-prefer-quality"
                        label="Keep the higher-quality file when the right track already has one"
                        title="Off: ask each time the right track already has a file (skipped conflicts are reported in batch relink)"
                        checked={preferQuality}
                        onChange={setPreferQuality}
                        className="max-w-[18rem] text-[11px] leading-tight"
                      />
                      <span>
                        <strong className="text-white">{selectedInView}</strong> of {filteredItems.length} files selected
                      </span>
                      <TapeDeckButton
                        size="sm"
                        disabled={checkDisabled}
                        title={checkTitle}
                        onClick={() => void startCheck()}
                        icon={
                          checkRunning || starting ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : (
                            <AudioLines className="h-3 w-3" />
                          )
                        }
                      >
                        Check all files
                      </TapeDeckButton>
                      <TapeDeckButton
                        size="sm"
                        onClick={handleReload}
                        icon={<RefreshCw className="h-3 w-3" />}
                        title="Reload wrong files"
                      >
                        Refresh
                      </TapeDeckButton>
                    </div>
                  </div>

                  {/* Collapsible timing note directly under the header toolbar */}
                  <div className="space-y-1.5">
                    <TapeDeckButton
                      size="sm"
                      onClick={() => setNoteExpanded((prev) => !prev)}
                      aria-expanded={noteExpanded}
                      icon={
                        <ChevronDown
                          className={`h-3 w-3 transition-transform ${noteExpanded ? 'rotate-180' : ''}`}
                        />
                      }
                    >
                      How long does this take?
                    </TapeDeckButton>
                    {noteExpanded && (
                      <p className="text-xs text-neutral-400 font-mono">
                        The first check listens to every file, about 100 a minute, so a large library takes several hours. You can close this window while it runs. Later checks only look at new or changed files.
                      </p>
                    )}
                  </div>

                  {statusAndErrors}

                  {/* Virtualized List Container */}
                  <div
                    ref={parentRef}
                    tabIndex={0}
                    role="region"
                    aria-label="Wrong files (scrollable)"
                    className="virtual-scroll overflow-y-auto border border-[#222222] rounded-[4px] bg-[#0d0d0d] divide-y divide-[#1c1c1c]"
                    style={{ maxHeight: 'min(450px, 55vh)' }}
                  >
                    <div
                      style={{
                        height: `${rowVirtualizer.getTotalSize()}px`,
                        width: '100%',
                        position: 'relative',
                      }}
                    >
                      {rowVirtualizer.getVirtualItems().map((virtualRow) => {
                        const item = filteredItems[virtualRow.index];
                        if (!item) return null;
                        const isSelected = selectedIds.has(item.file_id);
                        const rowBusy = relinkBusyId === item.file_id || deleteBusyId === item.file_id;
                        const filePath = item.relative_path || item.track_title;
                        const actualTrackId = item.actual_track_id;
                        const relinkTargetTitle = item.actual_track_title ?? item.actual_title ?? 'that track';
                        const relinkTargetArtist = item.actual_artist_name ?? item.actual_artist ?? 'that artist';

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
                            className={`p-2.5 transition-colors flex items-start gap-2.5 ${
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

                            <div className="min-w-0 flex-1">
                              <div
                                tabIndex={0}
                                role="group"
                                aria-label="File path and filed track (scrolls horizontally)"
                                className="overflow-x-auto pb-1 font-mono text-[11px] leading-tight"
                              >
                                <div className="w-max min-w-full space-y-1 whitespace-nowrap">
                                  <div className="flex items-center gap-1.5 text-neutral-400">
                                    <span className="shrink-0 text-[10px] uppercase text-neutral-500 font-bold">
                                      File:
                                    </span>
                                    <span title={filePath}>{filePath}</span>
                                  </div>
                                  <div className="flex items-center gap-1.5 text-neutral-300">
                                    <span className="shrink-0 text-[10px] uppercase text-neutral-500 font-bold">
                                      Filed as:
                                    </span>
                                    <span>
                                      &ldquo;{item.artist_name} — {item.track_title}&rdquo; ({item.album_title})
                                    </span>
                                  </div>
                                </div>
                              </div>

                              <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
                                <p className="flex items-start gap-1.5 break-words font-mono text-[11px] text-[var(--status-error)]">
                                  <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" />
                                  <span>
                                    Audio is actually: {item.actual_title ?? 'unknown track'}
                                    {item.actual_artist ? ` — ${item.actual_artist}` : ''}
                                  </span>
                                </p>
                                {actualTrackId ? (
                                  <TapeDeckButton
                                    size="sm"
                                    disabled={rowBusy || batchRelinking || batchDeleting}
                                    title={`Link this file to ${relinkTargetTitle} by ${relinkTargetArtist}`}
                                    onClick={() => void relink(item.file_id, actualTrackId)}
                                    icon={
                                      relinkBusyId === item.file_id ? (
                                        <Loader2 className="h-3 w-3 animate-spin" />
                                      ) : (
                                        <Link2 className="h-3 w-3" />
                                      )
                                    }
                                  >
                                    Relink to this track
                                  </TapeDeckButton>
                                ) : (
                                  <span className="font-mono text-[11px] text-[var(--text-muted)]">
                                    Not in your library — no track to relink to
                                  </span>
                                )}
                                <TapeDeckButton
                                  size="sm"
                                  disabled={rowBusy || batchRelinking || batchDeleting}
                                  onClick={() => setPendingDelete(item)}
                                  icon={
                                    deleteBusyId === item.file_id ? (
                                      <Loader2 className="h-3 w-3 animate-spin" />
                                    ) : (
                                      <Trash2 className="h-3 w-3" />
                                    )
                                  }
                                >
                                  Delete file
                                </TapeDeckButton>
                              </div>

                              <div className="tab-strip mt-1 h-4 overflow-x-auto">
                                <KindChip kind={item.kind} />
                              </div>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </div>
              )}
            </>
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
