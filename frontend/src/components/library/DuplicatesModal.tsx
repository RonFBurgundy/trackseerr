import React, { useMemo, useRef, useState } from 'react';
import { ChevronDown, FileCheck, Loader2, RefreshCw } from 'lucide-react';
import { useVirtualizer } from '@tanstack/react-virtual';
import type { DuplicateChoice, DuplicateFileRef, DuplicateResolveChoice, DuplicateResolveResponse } from '@/types/models';
import { useDuplicateChoices, type BatchSummary, type DuplicateKind } from '@/hooks/useDuplicateChoices';
import { errorMessage } from '@/services/apiClient';
import { ObsidianModal, SelectionCheckbox, TapeDeckButton } from '@/components/ui';
import { formatBytes } from '@/components/lists';
import { CHIP_BASE, CHIP_RISKY, CHIP_SAFE } from './libraryChips';

export interface DuplicatesModalProps {
  isOpen: boolean;
  onClose: () => void;
  /** Reports how many choices are still open after a change, so the menu label can follow. */
  onCountChange?: (count: number) => void;
  /** Called after a choice is applied, so the library can refresh. */
  onResolved?: () => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

const KIND_META: Record<DuplicateKind, { label: string; risky: boolean; help?: string }> = {
  duplicate_files: {
    label: 'Same tracks',
    risky: false,
  },
  ambiguous: {
    label: 'Needs a choice',
    risky: true,
    help: 'This album could belong to more than one existing album',
  },
};

const KIND_ORDER: readonly DuplicateKind[] = ['duplicate_files', 'ambiguous'];

function folderOf(path: string): string {
  const i = path.lastIndexOf('/');
  return i > 0 ? path.slice(0, i) : path;
}

function describeFile(f: DuplicateFileRef): string[] {
  const lines: string[] = [];
  const quality = [f.quality_name, f.codec ? f.codec.toUpperCase() : null].filter(Boolean).join(' · ');
  if (quality) lines.push(quality);
  const depth = [
    f.bits_per_sample ? `${f.bits_per_sample}-bit` : null,
    f.sample_rate ? `${parseFloat((f.sample_rate / 1000).toFixed(1))} kHz` : null,
  ]
    .filter(Boolean)
    .join(' / ');
  if (depth) lines.push(depth);
  if (f.bitrate) lines.push(`${Math.round(f.bitrate / 1000)} kbps`);
  if (f.size_bytes) lines.push(formatBytes(f.size_bytes));
  return lines;
}

const FileSide: React.FC<{ label: string; file: DuplicateFileRef }> = ({ label, file }) => (
  <div className="min-w-0 flex-1 flex flex-col gap-0.5">
    <span className="text-[10px] uppercase tracking-wider text-neutral-500">{label}</span>
    <span className="text-xs text-neutral-200 font-mono">{describeFile(file).join(' · ') || 'No details'}</span>
    <span className="text-[11px] text-neutral-500 font-mono truncate" title={file.file_path}>
      {folderOf(file.file_path)}
    </span>
  </div>
);

/** Plain-English line for what a choice did, using the kept and recycled file counts the server reports. */
function resolvedMessage(choice: DuplicateResolveChoice, item: DuplicateChoice, res: DuplicateResolveResponse): string {
  if (choice === 'keep_both') return 'Kept both albums as they are';
  if (res.recycled_file_ids.length === 0) {
    return res.merged ? `Merged "${item.source_title}" into "${item.holder_title}"` : 'Done';
  }
  const n = res.recycled_file_ids.length;
  const keptId = res.kept_file_ids[0];
  const kept = item.pairs
    .flatMap((p) => [p.holder_file, p.source_file])
    .find((f) => f.id === keptId);
  const keptLabel = kept
    ? [kept.codec ? kept.codec.toUpperCase() : null, kept.bits_per_sample && kept.sample_rate ? `${kept.bits_per_sample}/${parseFloat((kept.sample_rate / 1000).toFixed(1))}` : null]
        .filter(Boolean)
        .join(' ')
    : '';
  const head = keptLabel ? `Kept ${keptLabel}` : 'Kept the copy you chose';
  return `${head}, moved the other ${n === 1 ? 'copy' : `${n} copies`} to the recycle bin`;
}

function qualitySignature(f: DuplicateFileRef): string {
  return `${f.quality_name ?? ''}|${f.codec ?? ''}|${f.bits_per_sample ?? ''}|${f.sample_rate ?? ''}`;
}

export const DuplicatesModal: React.FC<DuplicatesModalProps> = ({ isOpen, onClose, onCountChange, onResolved, onToast }) => {
  const {
    items,
    filteredItems,
    isLoading,
    error,
    resolvingId,
    resolveError,
    selectedIds,
    toggleSelect,
    selectAll,
    selectNone,
    selectSafe,
    activeKinds,
    toggleKind,
    clearKinds,
    kindCounts,
    batchProgress,
    resolve,
    resolveMany,
    reload,
  } = useDuplicateChoices(isOpen);

  const [expandedIds, setExpandedIds] = useState<Set<number>>(() => new Set());
  const [batchSummary, setBatchSummary] = useState<BatchSummary | null>(null);
  const [activeBatchChoice, setActiveBatchChoice] = useState<DuplicateResolveChoice | null>(null);

  const parentRef = useRef<HTMLDivElement | null>(null);

  const rowVirtualizer = useVirtualizer({
    count: filteredItems.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 96,
    overscan: 8,
  });

  const toggleExpanded = (id: number): void => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  React.useEffect(() => {
    if (isOpen && !isLoading && !error) onCountChange?.(items.length);
  }, [isOpen, isLoading, error, items.length, onCountChange]);

  const selectedSafeCount = useMemo(
    () => items.reduce((n, item) => (selectedIds.has(item.id) && item.kind !== 'ambiguous' ? n + 1 : n), 0),
    [items, selectedIds]
  );

  const selectedInView = useMemo(
    () => filteredItems.reduce((n, item) => (selectedIds.has(item.id) ? n + 1 : n), 0),
    [filteredItems, selectedIds]
  );
  const allSelected = filteredItems.length > 0 && selectedInView === filteredItems.length;
  const batchRunning = batchProgress !== null;

  const visibleKinds = useMemo(
    () => KIND_ORDER.filter((kind) => kindCounts[kind] > 0),
    [kindCounts]
  );

  const handle = async (item: DuplicateChoice, choice: DuplicateResolveChoice): Promise<void> => {
    try {
      const res = await resolve(item.id, choice);
      onToast(resolvedMessage(choice, item, res), 'ok');
      onResolved?.();
    } catch (err: unknown) {
      // The server's own message (for example a 409) is shown inline below; toast it too so it is not missed.
      onToast(errorMessage(err, 'Could not resolve this duplicate'), 'error');
    }
  };

  const handleBatch = async (choice: DuplicateResolveChoice): Promise<void> => {
    if (batchRunning) return;
    setActiveBatchChoice(choice);
    setBatchSummary(null);
    try {
      const summary = await resolveMany(Array.from(selectedIds), choice);
      setBatchSummary(summary);
      onResolved?.();
      const summaryLine = `Resolved ${summary.resolved} · skipped ${summary.skipped} (needs a choice) · ${summary.errors} errors`;
      onToast(summaryLine, summary.errors > 0 ? 'error' : 'ok');
    } finally {
      setActiveBatchChoice(null);
    }
  };

  const handleReload = (): void => {
    setBatchSummary(null);
    reload();
  };

  const handleClose = (): void => {
    setBatchSummary(null);
    onClose();
  };

  const footer = (
    <>
      <TapeDeckButton onClick={handleClose} disabled={batchRunning}>
        {batchSummary ? 'Done' : 'Cancel'}
      </TapeDeckButton>
      <TapeDeckButton
        disabled={isLoading || batchRunning || selectedIds.size === 0}
        onClick={() => void handleBatch('keep_both')}
        icon={batchRunning && activeBatchChoice === 'keep_both' ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
      >
        {batchRunning && activeBatchChoice === 'keep_both'
          ? 'Keeping...'
          : `Keep both selected (${selectedIds.size})`}
      </TapeDeckButton>
      <TapeDeckButton
        variant="amber"
        disabled={isLoading || batchRunning || selectedSafeCount === 0}
        onClick={() => void handleBatch('prefer_quality')}
        icon={batchRunning && activeBatchChoice === 'prefer_quality' ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
      >
        {batchRunning && activeBatchChoice === 'prefer_quality'
          ? 'Sorting...'
          : `Keep better quality (${selectedSafeCount})`}
      </TapeDeckButton>
    </>
  );

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={handleClose}
      title="Duplicates"
      subtitle="Albums that turned out to be the same release"
      maxWidth="sm:max-w-4xl"
      footer={footer}
    >
      <div className="h-full sm:h-auto flex flex-col min-h-0 flex-1 gap-4 sm:block sm:flex-none sm:space-y-4">
        {isLoading && (
          <div className="flex flex-col items-center justify-center gap-2 py-14 text-xs font-mono uppercase tracking-widest text-[var(--text-secondary)]">
            <Loader2 className="h-6 w-6 animate-spin text-[var(--accent-amber)]" />
            <span>Loading duplicates…</span>
          </div>
        )}

        {error && (
          <div
            role="alert"
            className="p-3 rounded-[3px] border border-[var(--status-error)]/50 bg-[var(--status-error)]/10 text-xs font-mono text-[var(--status-error)] space-y-2"
          >
            <p>{error}</p>
            <div>
              <TapeDeckButton size="sm" onClick={handleReload}>
                Try again
              </TapeDeckButton>
            </div>
          </div>
        )}

        {!isLoading && !error && (
          <>
            {items.length === 0 ? (
              <div className="py-12 text-center space-y-2">
                <FileCheck className="h-8 w-8 text-[var(--accent-amber)] mx-auto opacity-70" />
                <p className="text-sm font-medium text-white">No duplicates to sort out</p>
              </div>
            ) : (
              <div className="flex flex-col min-h-0 flex-1 gap-3 sm:block sm:flex-none sm:space-y-3">
                <div className="shrink-0 space-y-1">
                  <p className="text-xs font-mono text-neutral-400">
                    These albums were merged by MusicBrainz match, but both had a file for the same track. Pick which copy to keep.
                    The copy you don&apos;t keep goes to the recycle bin.
                  </p>
                  <p className="text-xs font-mono text-neutral-400">
                    Showing <strong className="text-white">{filteredItems.length}</strong>{' '}
                    {filteredItems.length === 1 ? 'album' : 'albums'}
                    {filteredItems.length !== items.length ? ` of ${items.length}` : ''}
                  </p>
                </div>

                {/* Kind filter bar */}
                <div
                  role="group"
                  aria-label="Filter by kind"
                  className="shrink-0 tab-strip overflow-x-auto pb-0.5"
                >
                  {visibleKinds.map((kind) => {
                    const meta = KIND_META[kind];
                    const active = activeKinds.has(kind);
                    return (
                      <button
                        key={kind}
                        type="button"
                        aria-pressed={active}
                        title={meta.help}
                        onClick={() => toggleKind(kind)}
                        className={`${CHIP_BASE} cursor-pointer transition-colors ${
                          active
                            ? 'border-[#e5a00d] bg-[#e5a00d] text-black font-bold'
                            : meta.risky
                              ? `${CHIP_RISKY} hover:border-[#e5a00d]`
                              : `${CHIP_SAFE} hover:text-white hover:border-[#3a3a3a]`
                        }`}
                      >
                        {meta.label} · {kindCounts[kind]}
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
                <div className="shrink-0 flex flex-wrap items-center justify-between gap-2 border-b border-[#222222] pb-2 text-xs font-mono">
                  <div className="flex items-center gap-2">
                    <SelectionCheckbox
                      name="duplicates-select-all"
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
                      title="Select only albums with matching duplicate tracks"
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
                    <span>
                      <strong className="text-white">{selectedIds.size}</strong> of {items.length} albums selected
                    </span>
                    <TapeDeckButton
                      size="sm"
                      disabled={batchRunning}
                      onClick={handleReload}
                      icon={<RefreshCw className="h-3 w-3" />}
                      title="Reload duplicates"
                    >
                      Refresh
                    </TapeDeckButton>
                  </div>
                </div>

                {batchProgress && (
                  <div className="shrink-0 space-y-1 text-xs font-mono text-neutral-300" aria-live="polite">
                    <p>
                      Sorting duplicates… {batchProgress.done}/{batchProgress.total}
                    </p>
                    <div className="h-1 w-full overflow-hidden rounded-[2px] bg-[#1c1c1c]">
                      <div
                        role="progressbar"
                        aria-label="Duplicate resolve progress"
                        aria-valuemin={0}
                        aria-valuenow={batchProgress.done}
                        aria-valuemax={batchProgress.total}
                        className="h-full bg-[#e5a00d] transition-[width]"
                        style={{
                          width: `${batchProgress.total > 0 ? Math.min(100, (batchProgress.done / batchProgress.total) * 100) : 0}%`,
                        }}
                      />
                    </div>
                  </div>
                )}

                {resolveError && (
                  <p role="alert" className="shrink-0 text-xs font-mono text-[var(--status-error)]">
                    {resolveError}
                  </p>
                )}

                {batchSummary && (
                  <div className="shrink-0 space-y-1 text-xs font-mono text-neutral-300">
                    <p>
                      Resolved {batchSummary.resolved} · skipped {batchSummary.skipped} (needs a choice) ·{' '}
                      {batchSummary.errors} errors
                    </p>
                    {batchSummary.errorMessages.length > 0 && (
                      <ul className="text-[var(--status-error)] space-y-0.5 max-h-32 overflow-y-auto pl-4 list-disc">
                        {batchSummary.errorMessages.map((msg, i) => (
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
                  aria-label="Duplicate albums (scrollable)"
                  className="virtual-scroll flex-1 min-h-[16rem] sm:flex-none sm:max-h-[min(450px,55vh)] overflow-y-auto border border-[#222222] rounded-[4px] bg-[#0d0d0d] divide-y divide-[#1c1c1c]"
                >
                  <div
                    style={{
                      height: `${rowVirtualizer.getTotalSize()}px`,
                      width: '100%',
                      position: 'relative',
                    }}
                  >
                    {rowVirtualizer.getVirtualItems().map((virtualRow) => {
                      const item: DuplicateChoice = filteredItems[virtualRow.index];
                      const isSelected = selectedIds.has(item.id);
                      const busy = resolvingId === item.id || batchRunning;
                      const ambiguous = item.kind === 'ambiguous';
                      const isExpanded = expandedIds.has(item.id);
                      const firstPair = item.pairs[0];
                      const sameQuality =
                        firstPair !== undefined &&
                        item.pairs.every(
                          (p) =>
                            qualitySignature(p.holder_file) === qualitySignature(firstPair.holder_file) &&
                            qualitySignature(p.source_file) === qualitySignature(firstPair.source_file)
                        );
                      const meta = KIND_META[item.kind];

                      return (
                        <div
                          key={item.id}
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
                              name={`duplicate-${item.id}`}
                              checked={isSelected}
                              disabled={batchRunning}
                              onChange={() => toggleSelect(item.id)}
                              label={`Select ${item.source_title}`}
                              inline
                            />
                          </div>

                          <div className="min-w-0 flex-1 space-y-1.5">
                            <div className="min-w-0">
                              <span className="text-sm text-white font-medium block truncate">
                                {item.source_title} &rarr; {item.holder_title}
                              </span>
                              <span className="text-xs text-neutral-400 block truncate">
                                {item.source_artist === item.holder_artist
                                  ? item.holder_artist
                                  : `${item.source_artist} / ${item.holder_artist}`}
                              </span>
                            </div>

                            {item.pairs.length > 0 && !isExpanded && (
                              <div className="flex flex-col gap-0.5 font-mono text-[11px] min-w-0">
                                <span className="text-neutral-300">
                                  {item.pairs.length} {item.pairs.length === 1 ? 'track' : 'tracks'} in both
                                </span>
                                {sameQuality && firstPair && (
                                  <>
                                    <span className="text-neutral-400 block truncate">
                                      Existing: {describeFile(firstPair.holder_file).join(' · ') || 'No details'}
                                    </span>
                                    <span className="text-neutral-400 block truncate">
                                      Incoming: {describeFile(firstPair.source_file).join(' · ') || 'No details'}
                                    </span>
                                  </>
                                )}
                              </div>
                            )}

                            {ambiguous && (
                              <p className="text-xs text-neutral-400">
                                This album could belong to more than one existing album. Choose the one it should be merged into.
                              </p>
                            )}

                            <div className="flex flex-wrap gap-1.5">
                              {ambiguous ? (
                                <>
                                  <TapeDeckButton
                                    size="sm"
                                    variant="amber"
                                    disabled={busy}
                                    onClick={() => void handle(item, 'keep_existing')}
                                  >
                                    {resolvingId === item.id ? <Loader2 className="h-3 w-3 animate-spin" /> : 'Merge into this album'}
                                  </TapeDeckButton>
                                  <TapeDeckButton size="sm" disabled={busy} onClick={() => void handle(item, 'keep_both')}>
                                    Keep both
                                  </TapeDeckButton>
                                </>
                              ) : (
                                <>
                                  <TapeDeckButton size="sm" disabled={busy} onClick={() => void handle(item, 'keep_existing')}>
                                    Keep existing
                                  </TapeDeckButton>
                                  <TapeDeckButton size="sm" disabled={busy} onClick={() => void handle(item, 'keep_incoming')}>
                                    Keep incoming
                                  </TapeDeckButton>
                                  <TapeDeckButton
                                    size="sm"
                                    variant="amber"
                                    disabled={busy}
                                    onClick={() => void handle(item, 'prefer_quality')}
                                  >
                                    {resolvingId === item.id ? <Loader2 className="h-3 w-3 animate-spin" /> : 'Keep better quality'}
                                  </TapeDeckButton>
                                  <TapeDeckButton size="sm" disabled={busy} onClick={() => void handle(item, 'keep_both')}>
                                    Keep both
                                  </TapeDeckButton>
                                </>
                              )}
                              {item.pairs.length > 0 && (
                                <TapeDeckButton
                                  size="sm"
                                  onClick={() => toggleExpanded(item.id)}
                                  aria-expanded={isExpanded}
                                  icon={<ChevronDown className={`h-3.5 w-3.5 transition-transform ${isExpanded ? 'rotate-180' : ''}`} />}
                                >
                                  {isExpanded ? 'Hide tracks' : 'Show tracks'}
                                </TapeDeckButton>
                              )}
                            </div>

                            {isExpanded && item.pairs.length > 0 && (
                              <div className="flex flex-col gap-1.5 border-t border-[#222222] pt-2">
                                {item.pairs.map((pair) => (
                                  <div
                                    key={`${pair.source_file.id}-${pair.holder_file.id}`}
                                    className="flex flex-col gap-1 border-t border-[#222222] first:border-t-0 pt-1.5 first:pt-0"
                                  >
                                    <span className="text-xs text-neutral-300 truncate">{pair.holder_track.title}</span>
                                    <div className="flex flex-col sm:flex-row gap-2 sm:gap-4">
                                      <FileSide label="Existing" file={pair.holder_file} />
                                      <FileSide label="Incoming" file={pair.source_file} />
                                    </div>
                                  </div>
                                ))}
                              </div>
                            )}

                            <div className="tab-strip mt-1 h-4 overflow-x-auto">
                              <span
                                title={meta.help}
                                className={`${CHIP_BASE} ${meta.risky ? CHIP_RISKY : CHIP_SAFE}`}
                              >
                                {meta.label}
                              </span>
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
  );
};
