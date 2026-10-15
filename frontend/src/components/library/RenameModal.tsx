import React, { useMemo, useRef, useState } from 'react';
import { AlertTriangle, ArrowRight, AudioLines, Check, FileCheck, Link2, Loader2, RefreshCw, TriangleAlert } from 'lucide-react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { ObsidianModal, SelectionCheckbox, TactileSwitch, TapeDeckButton } from '@/components/ui';
import { useRenamePreview } from '@/hooks/useRenamePreview';
import { useRenameRelink } from '@/hooks/useRenameRelink';
import { useRenameVerify } from '@/hooks/useRenameVerify';
import {
  relinkRenameBatch,
  type RenamePreviewItem,
  type RenameRelinkBatchItem,
  type RenameRelinkFile,
} from '@/services/renameService';
import { errorMessage } from '@/services/apiClient';
import { reasonInfo } from './renameReasons';

export interface RenameScope {
  artistId?: string;
  artistName?: string;
  albumId?: string;
  albumTitle?: string;
}

export interface RenameModalProps {
  scope: RenameScope | null;
  onClose: () => void;
  onRenamed?: () => void;
}

function modalSubtitle(scope: RenameScope | null): string {
  if (!scope) return 'Preview proposed file path changes';
  if (scope.albumTitle) return `Album: ${scope.albumTitle}`;
  if (scope.artistName) return `Artist: ${scope.artistName}`;
  return 'Entire library';
}

function modalTitle(scope: RenameScope | null): string {
  if (!scope) return 'Rename Files';
  if (scope.albumTitle) return `Rename Album Files`;
  if (scope.artistName) return `Rename Artist Files`;
  return 'Rename Files';
}

interface PathDiff {
  prefix: string;
  changed: string;
  suffix: string;
}

/** Split `proposed` into the part shared with `current` (prefix/suffix) and the differing middle. */
function diffPaths(current: string, proposed: string): PathDiff {
  const max = Math.min(current.length, proposed.length);
  let start = 0;
  while (start < max && current[start] === proposed[start]) start += 1;
  let end = 0;
  while (
    end < max - start &&
    current[current.length - 1 - end] === proposed[proposed.length - 1 - end]
  ) {
    end += 1;
  }
  return {
    prefix: proposed.slice(0, start),
    changed: proposed.slice(start, proposed.length - end),
    suffix: proposed.slice(proposed.length - end),
  };
}

const CHIP_BASE =
  'inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-[3px] border px-1.5 py-0.5 font-mono text-[10px] leading-none';
const CHIP_SAFE = 'border-[#2a2a2a] bg-[#161616] text-neutral-400';
const CHIP_RISKY = 'border-[#e5a00d]/50 bg-[#e5a00d]/10 text-[#ffc94d]';

const ReasonChip: React.FC<{ code: string }> = ({ code }) => {
  const info = reasonInfo(code);
  return (
    <span title={info.help} className={`${CHIP_BASE} ${info.risky ? CHIP_RISKY : CHIP_SAFE}`}>
      {info.label}
    </span>
  );
};

const LOSSLESS_CODECS = new Set(['flac', 'alac', 'wav', 'aiff', 'ape', 'wv']);

/** Positive when `a` is the higher-quality file, negative when `b` is, 0 on a tie. */
function compareQuality(a: RenameRelinkFile, b: RenameRelinkFile): number {
  const lossless = (f: RenameRelinkFile): number =>
    LOSSLESS_CODECS.has((f.codec ?? '').toLowerCase()) ? 1 : 0;
  const steps: number[] = [
    lossless(a) - lossless(b),
    (a.bits_per_sample ?? 0) - (b.bits_per_sample ?? 0),
    (a.sample_rate ?? 0) - (b.sample_rate ?? 0),
    (a.bitrate ?? 0) - (b.bitrate ?? 0),
    (a.size_bytes ?? 0) - (b.size_bytes ?? 0),
  ];
  return steps.find((n) => n !== 0) ?? 0;
}

function baseName(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

function fileFacts(file: RenameRelinkFile): string[] {
  const facts: string[] = [];
  const format = [file.quality_name, file.codec].filter((v): v is string => !!v);
  if (format.length > 0) facts.push(format.join(' / '));
  if (file.bitrate != null) facts.push(`${Math.round(file.bitrate)} kbps`);
  const depth = [
    file.bits_per_sample != null ? `${file.bits_per_sample}-bit` : null,
    file.sample_rate != null ? `${Number((file.sample_rate / 1000).toFixed(1))} kHz` : null,
  ].filter((v): v is string => v !== null);
  if (depth.length > 0) facts.push(depth.join(' / '));
  if (file.size_bytes != null) facts.push(`${(file.size_bytes / (1024 * 1024)).toFixed(1)} MB`);
  return facts;
}

const RelinkFileCard: React.FC<{ heading: string; file: RenameRelinkFile; better: boolean }> = ({
  heading,
  file,
  better,
}) => (
  <div className="min-w-0 space-y-1.5 rounded-[4px] border border-[var(--border-default)] bg-[var(--bg-card)] p-3 font-mono text-xs">
    <div className="flex items-center justify-between gap-2">
      <span className="text-[10px] font-bold uppercase tracking-widest text-[var(--text-secondary)]">{heading}</span>
      {better && (
        <span className="rounded-[3px] border border-[#e5a00d]/50 bg-[#e5a00d]/10 px-1.5 py-0.5 text-[10px] leading-none text-[#ffc94d]">
          Higher quality
        </span>
      )}
    </div>
    <p className="break-all text-white" title={file.file_path}>
      {baseName(file.file_path)}
    </p>
    <ul className="space-y-0.5 text-[var(--text-secondary)]">
      {fileFacts(file).map((fact) => (
        <li key={fact}>{fact}</li>
      ))}
    </ul>
  </div>
);

export const RenameModal: React.FC<RenameModalProps> = ({ scope, onClose, onRenamed }) => {
  const isOpen = scope !== null;
  const {
    items,
    total,
    truncated,
    filteredItems,
    reasonCounts,
    activeReasons,
    toggleReason,
    clearReasons,
    selectSafe,
    allPreviewItems,
    loading,
    applying,
    error,
    selectedIds,
    toggleSelect,
    selectAll,
    selectNone,
    apply,
    result,
    reload,
    refreshFiles,
    resetResult,
    riskyIds,
    safetyWarnings,
  } = useRenamePreview({
    artistId: scope?.artistId,
    albumId: scope?.albumId,
    enabled: isOpen,
  });

  const verifyingFileIdsRef = useRef<string[]>([]);

  const {
    status: verifyStatus,
    starting: verifyStarting,
    error: verifyError,
    start: startVerify,
  } = useRenameVerify({
    enabled: isOpen,
    onFinished: () => {
      const ids = verifyingFileIdsRef.current;
      if (ids.length > 0) {
        void refreshFiles(ids);
        verifyingFileIdsRef.current = [];
      } else {
        void reload();
      }
    },
  });
  const verifyRunning = verifyStatus?.running === true;
  const keyMissing = verifyStatus?.api_key_set === false;
  const verifyDisabled = riskyIds.length === 0 || verifyRunning || verifyStarting || keyMissing;
  const verifyTitle = keyMissing
    ? 'Set an AcoustID API key in Settings → Media Management'
    : 'Fingerprint the audio of risky renames and compare it with the linked track';

  const {
    busyFileId,
    conflict,
    error: relinkError,
    preferQuality,
    setPreferQuality,
    relink,
    resolve: resolveConflict,
    dismissConflict,
  } = useRenameRelink({
    onRelinked: (fileId, res) => {
      const ids: string[] = [fileId];
      if (res.kept_file_id) ids.push(res.kept_file_id);
      if (res.recycled_file_id) ids.push(res.recycled_file_id);
      void refreshFiles(ids);
    },
  });
  const conflictOrder = conflict ? compareQuality(conflict.existing, conflict.incoming) : 0;

  const suggestedRelinkItems = useMemo(() => {
    return items.filter(
      (item) =>
        selectedIds.has(item.file_id) &&
        item.verification?.status === 'mismatch' &&
        Boolean(item.verification?.actual_track_id)
    );
  }, [items, selectedIds]);
  const suggestedCount = suggestedRelinkItems.length;

  const [batchRelinking, setBatchRelinking] = useState<boolean>(false);
  const [batchRelinkSummary, setBatchRelinkSummary] = useState<{
    relinked: number;
    conflictsSkipped: number;
    errors: number;
    errorMessages: string[];
  } | null>(null);
  const [batchRelinkError, setBatchRelinkError] = useState<string | null>(null);

  const handleBatchRelink = async () => {
    if (suggestedRelinkItems.length === 0 || batchRelinking) return;

    setBatchRelinking(true);
    setBatchRelinkError(null);
    setBatchRelinkSummary(null);

    const conflictChoice: 'prefer_quality' | 'skip' = preferQuality ? 'prefer_quality' : 'skip';
    const batchItems: RenameRelinkBatchItem[] = suggestedRelinkItems.map((item) => ({
      file_id: item.file_id,
      target_track_id: item.verification!.actual_track_id!,
    }));

    const BATCH_SIZE = 500;
    const chunks: RenameRelinkBatchItem[][] = [];
    for (let i = 0; i < batchItems.length; i += BATCH_SIZE) {
      chunks.push(batchItems.slice(i, i + BATCH_SIZE));
    }

    let relinked = 0;
    let conflictsSkipped = 0;
    let errors = 0;
    const errorMessages: string[] = [];
    const affectedIds = new Set<string>();

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
          if (r.file_id) affectedIds.add(r.file_id);
          if (r.kept_file_id) affectedIds.add(r.kept_file_id);
          if (r.recycled_file_id) affectedIds.add(r.recycled_file_id);
          if (r.status === 'error' && r.message) {
            errorMessages.push(r.message);
          }
        }
      }

      setBatchRelinkSummary({
        relinked,
        conflictsSkipped,
        errors,
        errorMessages,
      });

      if (affectedIds.size > 0) {
        await refreshFiles(Array.from(affectedIds));
      }
    } catch (err: unknown) {
      setBatchRelinkError(errorMessage(err, 'Failed to relink suggested tracks'));
    } finally {
      setBatchRelinking(false);
    }
  };

  const handleReload = async () => {
    setBatchRelinkSummary(null);
    setBatchRelinkError(null);
    await reload();
  };

  const parentRef = useRef<HTMLDivElement | null>(null);

  const rowVirtualizer = useVirtualizer({
    count: filteredItems.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 92,
    overscan: 8,
  });

  const selectedInView = filteredItems.reduce(
    (n, item) => (selectedIds.has(item.file_id) ? n + 1 : n),
    0
  );
  const allSelected = filteredItems.length > 0 && selectedInView === filteredItems.length;
  const reasonCodes = Object.keys(reasonCounts).sort((a, b) => reasonCounts[b] - reasonCounts[a]);

  const handleApply = async () => {
    const res = await apply();
    if (res && res.renamed_count > 0 && onRenamed) {
      onRenamed();
    }
  };

  const handleClose = () => {
    setBatchRelinkSummary(null);
    setBatchRelinkError(null);
    resetResult();
    onClose();
  };

  const footer = (
    <>
      <TapeDeckButton onClick={handleClose}>
        {result ? 'Done' : 'Cancel'}
      </TapeDeckButton>
      {!result && (
        <TapeDeckButton
          variant="amber"
          disabled={loading || applying || selectedIds.size === 0}
          onClick={() => void handleApply()}
          icon={applying ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileCheck className="h-4 w-4" />}
        >
          {applying ? 'Renaming...' : `Rename selected (${selectedIds.size})`}
        </TapeDeckButton>
      )}
    </>
  );

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={handleClose}
      title={modalTitle(scope)}
      subtitle={modalSubtitle(scope)}
      maxWidth="sm:max-w-4xl"
      footer={footer}
    >
      <div className="space-y-4">
        {loading && (
          <div className="flex flex-col items-center justify-center gap-2 py-14 text-xs font-mono uppercase tracking-widest text-[var(--text-secondary)]">
            <Loader2 className="h-6 w-6 animate-spin text-[var(--accent-amber)]" />
            <span>Scanning filenames against naming pattern...</span>
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

        {/* Success / Result State */}
        {result && (
          <div className="space-y-3">
            <div className="p-4 rounded-[4px] border border-[var(--border-default)] bg-[#141414] space-y-2">
              <div className="flex items-center gap-2 text-sm font-semibold text-[var(--status-success)]">
                <Check className="h-4 w-4" />
                <span>Rename completed</span>
              </div>
              <p className="text-xs font-mono text-[var(--text-secondary)]">
                Successfully renamed <span className="text-white font-bold">{result.renamed_count}</span> file
                {result.renamed_count === 1 ? '' : 's'}.
              </p>
            </div>

            {result.blocked_count > 0 && (
              <p className="p-3 rounded-[3px] border border-[#e5a00d]/40 bg-[#e5a00d]/10 text-xs font-mono text-[#ffc94d]">
                {result.blocked_count} file{result.blocked_count === 1 ? '' : 's'} skipped — the audio is a
                different track
              </p>
            )}

            {result.errors.length > 0 && (
              <div className="p-3 rounded-[3px] border border-[var(--status-error)]/40 bg-[var(--status-error)]/10 space-y-1.5">
                <p className="text-xs font-bold text-[var(--status-error)] uppercase font-mono">
                  Errors ({result.errors.length}):
                </p>
                <ul className="text-xs font-mono text-neutral-300 space-y-1 max-h-40 overflow-y-auto pl-4 list-disc">
                  {result.errors.map((err, idx) => (
                    <li key={idx} className="break-all">
                      {err}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        {/* Preview Ready State */}
        {!loading && !result && (
          <>
            {items.length === 0 ? (
              <div className="py-12 text-center space-y-2">
                <FileCheck className="h-8 w-8 text-[var(--accent-amber)] mx-auto opacity-70" />
                <p className="text-sm font-medium text-white">All files match the naming template</p>
                <p className="text-xs text-neutral-400 font-mono">
                  {allPreviewItems.length > 0
                    ? `All ${allPreviewItems.length} scanned files are already organized in their destination paths.`
                    : 'No library files found to rename.'}
                </p>
              </div>
            ) : (
              <div className="space-y-3">
                {safetyWarnings.length > 0 && (
                  <div
                    role="note"
                    className="p-3 rounded-[3px] border border-[var(--accent-amber)]/50 bg-[var(--accent-amber)]/10 text-xs font-mono text-[var(--accent-amber)] space-y-1"
                  >
                    <div className="flex items-start gap-2">
                      <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
                      <div className="space-y-1">
                        {safetyWarnings.map((warning, idx) => (
                          <p key={idx} className="text-xs font-mono">
                            {warning}
                          </p>
                        ))}
                      </div>
                    </div>
                  </div>
                )}

                <p className="text-xs font-mono text-neutral-400">
                  {truncated ? (
                    <>
                      Showing <strong className="text-white">{items.length}</strong> of{' '}
                      <strong className="text-[var(--accent-amber)]">{total}</strong> proposed moves — apply these, then run the
                      preview again for the rest.
                    </>
                  ) : (
                    <>
                      Showing <strong className="text-white">{items.length}</strong> proposed move
                      {items.length === 1 ? '' : 's'}
                    </>
                  )}
                </p>

                {/* Reason filter bar */}
                <div
                  role="group"
                  aria-label="Filter by reason"
                  className="tab-strip overflow-x-auto pb-0.5"
                >
                  {reasonCodes.map((code) => {
                    const info = reasonInfo(code);
                    const active = activeReasons.has(code);
                    return (
                      <button
                        key={code}
                        type="button"
                        aria-pressed={active}
                        title={info.help}
                        onClick={() => toggleReason(code)}
                        className={`${CHIP_BASE} cursor-pointer transition-colors ${
                          active
                            ? 'border-[#e5a00d] bg-[#e5a00d] text-black font-bold'
                            : info.risky
                              ? `${CHIP_RISKY} hover:border-[#e5a00d]`
                              : `${CHIP_SAFE} hover:text-white hover:border-[#3a3a3a]`
                        }`}
                      >
                        {info.label} · {reasonCounts[code]}
                      </button>
                    );
                  })}
                  {activeReasons.size > 0 && (
                    <button
                      type="button"
                      onClick={clearReasons}
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
                      name="rename-select-all"
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
                      title="Select only the cosmetic, low-risk moves"
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
                      name="rename-relink-prefer-quality"
                      label="Keep the higher-quality file when the right track already has one"
                      title="Off: ask each time the right track already has a file (skipped conflicts are reported in batch relink)"
                      checked={preferQuality}
                      onChange={setPreferQuality}
                      className="max-w-[18rem] text-[11px] leading-tight"
                    />
                    <span>
                      <strong className="text-white">{selectedIds.size}</strong> of {items.length} files selected
                    </span>
                    <TapeDeckButton
                      size="sm"
                      disabled={verifyDisabled}
                      title={verifyTitle}
                      onClick={() => {
                        verifyingFileIdsRef.current = riskyIds;
                        void startVerify(riskyIds);
                      }}
                      icon={
                        verifyRunning || verifyStarting ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          <AudioLines className="h-3 w-3" />
                        )
                      }
                    >
                      Verify risky ({riskyIds.length})
                    </TapeDeckButton>
                    <TapeDeckButton
                      size="sm"
                      disabled={suggestedCount === 0 || batchRelinking}
                      title={
                        suggestedCount === 0
                          ? 'No selected audio mismatches with known library tracks'
                          : `Relink ${suggestedCount} selected audio mismatches to their identified tracks`
                      }
                      onClick={() => void handleBatchRelink()}
                      icon={
                        batchRelinking ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          <Link2 className="h-3 w-3" />
                        )
                      }
                    >
                      Relink suggested ({suggestedCount})
                    </TapeDeckButton>
                    <TapeDeckButton size="sm" onClick={() => void handleReload()} icon={<RefreshCw className="h-3 w-3" />} title="Reload preview">
                      Refresh
                    </TapeDeckButton>
                  </div>
                </div>

                {verifyRunning && verifyStatus && (
                  <div className="space-y-1 text-xs font-mono text-neutral-300" aria-live="polite">
                    <p>
                      Verifying audio… {verifyStatus.done}/{verifyStatus.total}
                    </p>
                    <div className="h-1 w-full overflow-hidden rounded-[2px] bg-[#1c1c1c]">
                      <div
                        role="progressbar"
                        aria-label="Audio verification progress"
                        aria-valuemin={0}
                        aria-valuenow={verifyStatus.done}
                        aria-valuemax={verifyStatus.total}
                        className="h-full bg-[#e5a00d] transition-[width]"
                        style={{
                          width: `${verifyStatus.total > 0 ? Math.min(100, (verifyStatus.done / verifyStatus.total) * 100) : 0}%`,
                        }}
                      />
                    </div>
                  </div>
                )}
                {!verifyRunning && verifyStatus && verifyStatus.finished_at != null && (
                  <div className="space-y-0.5 text-xs font-mono text-neutral-400">
                    <p>
                      {verifyStatus.verified} verified · {verifyStatus.mismatched} mismatched ·{' '}
                      {verifyStatus.unknown} unknown · {verifyStatus.errors} errors
                    </p>
                    {verifyStatus.message && <p className="text-[#ffc94d]">{verifyStatus.message}</p>}
                  </div>
                )}
                {verifyError && (
                  <p role="alert" className="text-xs font-mono text-[var(--status-error)]">
                    {verifyError}
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

                {/* Virtualized List Container */}
                <div
                  ref={parentRef}
                  tabIndex={0}
                  role="region"
                  aria-label="Rename preview files (scrollable)"
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
                      const item: RenamePreviewItem = filteredItems[virtualRow.index];
                      const isSelected = selectedIds.has(item.file_id);
                      const diff = diffPaths(item.current_path, item.proposed_path);
                      const mismatch = item.verification?.status === 'mismatch';

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
                          <div className="pt-0.5 shrink-0" title={mismatch ? 'Blocked: the audio is a different track' : undefined}>
                            <SelectionCheckbox
                              name={`rename-file-${item.file_id}`}
                              checked={isSelected}
                              disabled={mismatch}
                              onChange={() => toggleSelect(item.file_id)}
                              label={`Select rename for ${item.proposed_path}`}
                              inline
                            />
                          </div>

                          <div className="min-w-0 flex-1">
                            <div
                              tabIndex={0}
                              role="group"
                              aria-label="Current and proposed path (scrolls horizontally)"
                              className="overflow-x-auto pb-1 font-mono text-[11px] leading-tight"
                            >
                              <div className="w-max min-w-full space-y-1 whitespace-nowrap">
                                <div className="flex items-center gap-1.5 text-neutral-400">
                                  <span className="shrink-0 text-[10px] uppercase text-neutral-500 font-bold">From:</span>
                                  <span title={item.current_path}>{item.current_path}</span>
                                </div>
                                <div className="flex items-center gap-1.5 text-[#e5a00d]/80">
                                  <ArrowRight className="h-3 w-3 shrink-0 text-[#e5a00d]/70" />
                                  <span className="shrink-0 text-[10px] uppercase text-[#e5a00d]/80 font-bold">To:</span>
                                  <span title={item.proposed_path}>
                                    {diff.prefix}
                                    <mark className="rounded-[2px] bg-[#e5a00d]/25 px-0.5 font-semibold text-[#ffc94d]">
                                      {diff.changed}
                                    </mark>
                                    {diff.suffix}
                                  </span>
                                </div>
                              </div>
                            </div>
                            {mismatch && item.verification && (
                              <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
                                <p className="flex items-start gap-1.5 break-words font-mono text-[11px] text-[var(--status-error)]">
                                  <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" />
                                  <span>
                                    Audio is actually: {item.verification.actual_title ?? 'unknown track'}
                                    {item.verification.actual_artist ? ` — ${item.verification.actual_artist}` : ''}
                                  </span>
                                </p>
                                {item.verification.actual_track_id ? (
                                  <TapeDeckButton
                                    size="sm"
                                    disabled={busyFileId === item.file_id}
                                    title={`Link this file to ${item.verification.actual_title ?? 'that track'} instead of its current track`}
                                    onClick={() => {
                                      const target = item.verification?.actual_track_id;
                                      if (target) void relink(item.file_id, target);
                                    }}
                                    icon={
                                      busyFileId === item.file_id ? (
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
                              </div>
                            )}
                            <div className="tab-strip mt-1 h-4 overflow-x-auto">
                              {item.reasons.map((code) => (
                                <ReasonChip key={code} code={code} />
                              ))}
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

      <ObsidianModal
        isOpen={conflict !== null}
        onClose={dismissConflict}
        title="The right track already has a file"
        subtitle="Choose which file stays linked to that track"
        maxWidth="sm:max-w-2xl"
        footer={
          <>
            <TapeDeckButton onClick={dismissConflict} disabled={busyFileId !== null}>
              Cancel
            </TapeDeckButton>
            <TapeDeckButton disabled={busyFileId !== null} onClick={() => void resolveConflict('keep_existing')}>
              Keep existing
            </TapeDeckButton>
            <TapeDeckButton
              variant="amber"
              disabled={busyFileId !== null}
              onClick={() => void resolveConflict('keep_incoming')}
              icon={busyFileId !== null ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
            >
              Keep this file
            </TapeDeckButton>
          </>
        }
      >
        {conflict && (
          <div className="space-y-3">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <RelinkFileCard heading="Already on that track" file={conflict.existing} better={conflictOrder > 0} />
              <RelinkFileCard heading="This file" file={conflict.incoming} better={conflictOrder < 0} />
            </div>
            <p className="font-mono text-xs text-[var(--text-secondary)]">
              The file you don't keep goes to the recycle bin.
            </p>
          </div>
        )}
      </ObsidianModal>
    </ObsidianModal>
  );
};
