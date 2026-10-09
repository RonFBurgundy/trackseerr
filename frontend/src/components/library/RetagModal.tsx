import React, { useId, useRef } from 'react';
import { ArrowRight, Check, Image, Loader2, RefreshCw, Tag } from 'lucide-react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import { useRetagPreview } from '@/hooks/useRetagPreview';
import type { RetagPreviewItem, RetagFieldDiff } from '@/services/retagService';

export interface RetagScope {
  artistId?: string;
  artistName?: string;
  albumId?: string;
  albumTitle?: string;
}

export interface RetagModalProps {
  scope: RetagScope | null;
  onClose: () => void;
  onRetagged?: () => void;
}

function modalSubtitle(scope: RetagScope | null): string {
  if (!scope) return 'Preview proposed tag changes';
  if (scope.albumTitle) return `Album: ${scope.albumTitle}`;
  if (scope.artistName) return `Artist: ${scope.artistName}`;
  return 'Entire library';
}

function modalTitle(scope: RetagScope | null): string {
  if (!scope) return 'Retag Files';
  if (scope.albumTitle) return 'Retag Album Files';
  if (scope.artistName) return 'Retag Artist Files';
  return 'Retag Files';
}

function formatFieldName(field: string): string {
  switch (field) {
    case 'tracknumber':
      return 'Track #';
    case 'totaltracks':
      return 'Total Tracks';
    case 'discnumber':
      return 'Disc #';
    case 'totaldiscs':
      return 'Total Discs';
    case 'musicbrainz_artistid':
      return 'MB Artist ID';
    case 'musicbrainz_albumid':
      return 'MB Album ID';
    case 'musicbrainz_releasegroupid':
      return 'MB Release Group ID';
    case 'musicbrainz_trackid':
      return 'MB Track ID';
    default:
      return field.charAt(0).toUpperCase() + field.slice(1);
  }
}

export const RetagModal: React.FC<RetagModalProps> = ({ scope, onClose, onRetagged }) => {
  const selectAllId = useId();
  const embedArtId = useId();
  const isOpen = scope !== null;

  const {
    items,
    loading,
    applying,
    error,
    selectedIds,
    embedArt,
    setEmbedArt,
    toggleSelect,
    selectAll,
    selectNone,
    apply,
    result,
    reload,
    resetResult,
  } = useRetagPreview({
    artistId: scope?.artistId,
    albumId: scope?.albumId,
    enabled: isOpen,
  });

  const parentRef = useRef<HTMLDivElement | null>(null);

  const rowVirtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 110,
    overscan: 6,
  });

  const selectableItems = items.filter((item) => !item.skipped_reason);
  const allSelected = selectableItems.length > 0 && selectedIds.size === selectableItems.length;
  const isIndeterminate = selectedIds.size > 0 && !allSelected;

  const handleApply = async () => {
    const res = await apply();
    if (res && res.retagged_count > 0 && onRetagged) {
      onRetagged();
    }
  };

  const handleClose = () => {
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
          icon={applying ? <Loader2 className="h-4 w-4 animate-spin" /> : <Tag className="h-4 w-4" />}
        >
          {applying ? 'Retagging...' : `Retag selected (${selectedIds.size})`}
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
            <span>Scanning library audio tags against metadata...</span>
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
                <span>Retag process completed</span>
              </div>
              <p className="text-xs font-mono text-[var(--text-secondary)]">
                Successfully retagged <span className="text-white font-bold">{result.retagged_count}</span> file
                {result.retagged_count === 1 ? '' : 's'}.
                {result.skipped_count > 0 && (
                  <span className="text-[var(--accent-amber)] ml-2">
                    ({result.skipped_count} skipped)
                  </span>
                )}
              </p>
            </div>

            {/* Per-file results */}
            {(result.results ?? []).length > 0 && (
              <div className="border border-[#222222] rounded-[4px] bg-[#0d0d0d] overflow-hidden max-h-60 overflow-y-auto divide-y divide-[#1c1c1c] text-xs font-mono">
                {(result.results ?? []).map((res) => (
                  <div key={res.file_id} className="p-2.5 flex items-center justify-between gap-3">
                    <span className="text-neutral-300 truncate font-mono text-[11px]">{res.file_id}</span>
                    <div className="flex items-center gap-2 shrink-0">
                      {res.status === 'ok' && (
                        <span className="px-1.5 py-0.5 rounded-[2px] bg-green-950/80 border border-green-800/60 text-green-400 text-[10px] font-bold uppercase">
                          OK
                        </span>
                      )}
                      {res.status === 'skipped' && (
                        <span
                          className="px-1.5 py-0.5 rounded-[2px] bg-amber-950/80 border border-amber-800/60 text-amber-400 text-[10px] font-bold uppercase"
                          title={res.reason || undefined}
                        >
                          Skipped: {res.reason || 'Protected'}
                        </span>
                      )}
                      {res.status === 'error' && (
                        <span
                          className="px-1.5 py-0.5 rounded-[2px] bg-red-950/80 border border-red-800/60 text-red-400 text-[10px] font-bold uppercase"
                          title={res.reason || undefined}
                        >
                          Error: {res.reason || 'Failed'}
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {(result.errors ?? []).length > 0 && (
              <div className="p-3 rounded-[3px] border border-[var(--status-error)]/40 bg-[var(--status-error)]/10 space-y-1.5">
                <p className="text-xs font-bold text-[var(--status-error)] uppercase font-mono">
                  Errors ({(result.errors ?? []).length}):
                </p>
                <ul className="text-xs font-mono text-neutral-300 space-y-1 max-h-40 overflow-y-auto pl-4 list-disc">
                  {(result.errors ?? []).map((err, idx) => (
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
                <Tag className="h-8 w-8 text-[var(--accent-amber)] mx-auto opacity-70" />
                <p className="text-sm font-medium text-white">All audio tags match library metadata</p>
                <p className="text-xs text-neutral-400 font-mono">
                  No differing tags found between audio files and the catalog.
                </p>
              </div>
            ) : (
              <div className="space-y-3">
                {/* Header Toolbar */}
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[#222222] pb-2 text-xs font-mono">
                  <div className="flex items-center gap-2">
                    <input
                      id={selectAllId}
                      name="retag-select-all"
                      type="checkbox"
                      checked={allSelected}
                      ref={(el) => {
                        if (el) el.indeterminate = isIndeterminate;
                      }}
                      onChange={(e) => {
                        if (e.target.checked) selectAll();
                        else selectNone();
                      }}
                      className="rounded-[2px] bg-[#0d0d0d] border-[#2a2a2a] text-[#e5a00d] focus:ring-0 cursor-pointer"
                    />
                    <label htmlFor={selectAllId} className="cursor-pointer text-neutral-300">
                      Select all
                    </label>
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

                  <div className="flex items-center gap-3">
                    <div className="flex items-center gap-1.5 text-neutral-300">
                      <input
                        id={embedArtId}
                        name="retag-embed-art"
                        type="checkbox"
                        checked={embedArt}
                        onChange={(e) => setEmbedArt(e.target.checked)}
                        className="rounded-[2px] bg-[#0d0d0d] border-[#2a2a2a] text-[#e5a00d] focus:ring-0 cursor-pointer"
                      />
                      <label htmlFor={embedArtId} className="cursor-pointer flex items-center gap-1 text-xs">
                        <Image className="h-3.5 w-3.5 text-neutral-400" />
                        <span>Embed artwork</span>
                      </label>
                    </div>

                    <span className="text-neutral-500">•</span>

                    <div className="flex items-center gap-2 text-neutral-400">
                      <span>
                        <strong className="text-white">{selectedIds.size}</strong> of {items.length} files selected
                      </span>
                      <TapeDeckButton
                        size="sm"
                        onClick={() => void reload()}
                        icon={<RefreshCw className="h-3 w-3" />}
                        title="Reload preview"
                      >
                        Refresh
                      </TapeDeckButton>
                    </div>
                  </div>
                </div>

                {/* Virtualized List Container */}
                <div
                  ref={parentRef}
                  tabIndex={0}
                  role="region"
                  aria-label="Retag preview files (scrollable)"
                  className="virtual-scroll overflow-y-auto border border-[#222222] rounded-[4px] bg-[#0d0d0d] divide-y divide-[#1c1c1c]"
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
                      const item: RetagPreviewItem = items[virtualRow.index];
                      const isSelected = selectedIds.has(item.file_id);
                      const isSkipped = Boolean(item.skipped_reason);
                      const checkboxId = `retag-item-${item.file_id}`;
                      const diffList: RetagFieldDiff[] = item.changes ?? item.diffs ?? [];

                      return (
                        <div
                          key={item.file_id}
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
                            <input
                              id={checkboxId}
                              name={`retag-file-${item.file_id}`}
                              type="checkbox"
                              checked={isSelected}
                              disabled={isSkipped}
                              onChange={() => toggleSelect(item.file_id)}
                              aria-label={`Select retag for ${item.path}`}
                              className="rounded-[2px] bg-[#0d0d0d] border-[#2a2a2a] text-[#e5a00d] focus:ring-0 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                            />
                            <label htmlFor={checkboxId} className="sr-only">
                              Select retag for {item.path}
                            </label>
                          </div>

                          <div className="min-w-0 flex-1 space-y-1.5 font-mono text-[11px] leading-tight">
                            <div className="flex items-center justify-between gap-2">
                              <span className="font-semibold text-neutral-200 truncate" title={item.path}>
                                {item.path}
                              </span>
                              {isSkipped && (
                                <span className="px-1.5 py-0.5 rounded-[2px] bg-amber-950/80 border border-amber-800/60 text-amber-400 text-[10px] font-bold uppercase shrink-0">
                                  {item.skipped_reason}
                                </span>
                              )}
                            </div>

                            {/* Diff Table */}
                            {diffList.length > 0 && (
                              <div className="border border-[#262626] rounded-[3px] bg-[#0a0a0a] overflow-hidden">
                                <table className="w-full text-left text-[10px]">
                                  <thead className="bg-[#141414] text-neutral-400 border-b border-[#222222]">
                                    <tr>
                                      <th className="py-1 px-2 font-semibold">Field</th>
                                      <th className="py-1 px-2 font-semibold">Current</th>
                                      <th className="py-1 px-2 font-semibold">Proposed</th>
                                    </tr>
                                  </thead>
                                  <tbody className="divide-y divide-[#1a1a1a]">
                                    {diffList.map((diff, dIdx) => (
                                      <tr key={dIdx} className="hover:bg-[#121212]/60">
                                        <td className="py-1 px-2 font-medium text-neutral-400 whitespace-nowrap">
                                          {formatFieldName(diff.field)}
                                        </td>
                                        <td className="py-1 px-2 text-neutral-400 truncate max-w-[180px]" title={diff.current || undefined}>
                                          {diff.current ? diff.current : <span className="text-neutral-600 italic">(empty)</span>}
                                        </td>
                                        <td className="py-1 px-2 text-[#e5a00d] font-medium truncate max-w-[180px]" title={diff.proposed || undefined}>
                                          <div className="flex items-center gap-1">
                                            <ArrowRight className="h-2.5 w-2.5 text-[#e5a00d]/70 shrink-0" />
                                            <span>{diff.proposed || <span className="italic opacity-60">(empty)</span>}</span>
                                          </div>
                                        </td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            )}
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
