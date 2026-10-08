import React, { useId, useRef } from 'react';
import { ArrowRight, Check, FileCheck, Loader2, RefreshCw } from 'lucide-react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import { useRenamePreview } from '@/hooks/useRenamePreview';
import type { RenamePreviewItem } from '@/services/renameService';

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

export const RenameModal: React.FC<RenameModalProps> = ({ scope, onClose, onRenamed }) => {
  const selectAllId = useId();
  const isOpen = scope !== null;
  const {
    items,
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
    resetResult,
  } = useRenamePreview({
    artistId: scope?.artistId,
    albumId: scope?.albumId,
    enabled: isOpen,
  });

  const parentRef = useRef<HTMLDivElement | null>(null);

  const rowVirtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 76,
    overscan: 8,
  });

  const allSelected = items.length > 0 && selectedIds.size === items.length;
  const isIndeterminate = selectedIds.size > 0 && !allSelected;

  const handleApply = async () => {
    const res = await apply();
    if (res && res.renamed_count > 0 && onRenamed) {
      onRenamed();
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
                {/* Header Toolbar */}
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#222222] pb-2 text-xs font-mono">
                  <div className="flex items-center gap-2">
                    <input
                      id={selectAllId}
                      name="rename-select-all"
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
                  <div className="flex items-center gap-2 text-neutral-400">
                    <span>
                      <strong className="text-white">{selectedIds.size}</strong> of {items.length} files selected
                    </span>
                    <TapeDeckButton size="sm" onClick={() => void reload()} icon={<RefreshCw className="h-3 w-3" />} title="Reload preview">
                      Refresh
                    </TapeDeckButton>
                  </div>
                </div>

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
                      const item: RenamePreviewItem = items[virtualRow.index];
                      const isSelected = selectedIds.has(item.file_id);
                      const checkboxId = `rename-item-${item.file_id}`;

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
                              name={`rename-file-${item.file_id}`}
                              type="checkbox"
                              checked={isSelected}
                              onChange={() => toggleSelect(item.file_id)}
                              aria-label={`Select rename for ${item.proposed_path}`}
                              className="rounded-[2px] bg-[#0d0d0d] border-[#2a2a2a] text-[#e5a00d] focus:ring-0 cursor-pointer"
                            />
                            <label htmlFor={checkboxId} className="sr-only">
                              Select rename for {item.proposed_path}
                            </label>
                          </div>

                          <div className="min-w-0 flex-1 space-y-1 font-mono text-[11px] leading-tight">
                            <div className="flex items-center gap-1.5 text-neutral-400 break-all">
                              <span className="shrink-0 text-[10px] uppercase text-neutral-500 font-bold">From:</span>
                              <span className="truncate" title={item.current_path}>{item.current_path}</span>
                            </div>
                            <div className="flex items-center gap-1.5 text-[#e5a00d] break-all">
                              <ArrowRight className="h-3 w-3 shrink-0 text-[#e5a00d]/70" />
                              <span className="shrink-0 text-[10px] uppercase text-[#e5a00d]/80 font-bold">To:</span>
                              <span className="truncate font-semibold" title={item.proposed_path}>{item.proposed_path}</span>
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
