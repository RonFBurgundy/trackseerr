import React, { useId } from 'react';
import { Check, FolderSearch, Loader2 } from 'lucide-react';
import { useManualImport } from '@/hooks/useManualImport';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import type { ManualImportScope } from '@/types/manualImport';
import { ManualImportRowView } from './ManualImportRowView';

export interface ManualImportModalProps {
  /** Null keeps the modal closed. */
  scope: ManualImportScope | null;
  onClose: () => void;
  /** Called after files were imported so the parent can refresh its data. */
  onImported: () => void;
}

const labelClass = 'block text-[10px] uppercase tracking-widest font-mono text-[var(--text-muted)] mb-1';

function subtitleFor(scope: ManualImportScope | null): string | undefined {
  if (!scope) return undefined;
  if (scope.kind === 'folder') return 'Pick files from a folder';
  return scope.title;
}

export const ManualImportModal: React.FC<ManualImportModalProps> = ({ scope, onClose, onImported }) => {
  const folderId = useId();
  const mi = useManualImport({ scope, isOpen: scope !== null, onImported });
  const busy = mi.committing || mi.scanning;

  const footer = (
    <>
      <TapeDeckButton onClick={onClose}>Close</TapeDeckButton>
      <TapeDeckButton
        variant="amber"
        disabled={!mi.canCommit}
        onClick={() => void mi.commit()}
        icon={mi.committing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
      >
        Import selected ({mi.selectedCount})
      </TapeDeckButton>
    </>
  );

  return (
    <ObsidianModal
      isOpen={scope !== null}
      onClose={onClose}
      title="Manual import"
      subtitle={subtitleFor(scope)}
      maxWidth="sm:max-w-3xl"
      footer={footer}
    >
      <div className="space-y-3">
        {mi.needsFolderInput && (
          <div>
            <label htmlFor={folderId} className={labelClass}>
              Folder path (empty uses the staging folder)
            </label>
            <div className="flex items-center gap-1.5">
              <input
                id={folderId}
                name="manual-import-folder"
                type="text"
                value={mi.folderPath}
                onChange={(e) => mi.setFolderPath(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void mi.scan();
                }}
                disabled={mi.scanning || mi.committing}
                placeholder="/downloads/complete/album"
                className="w-full min-h-[36px] px-2.5 rounded-[3px] bg-[var(--bg-surface-elevated)] border border-[var(--border-default)] text-[13px] text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-amber)] disabled:opacity-50"
              />
              <TapeDeckButton
                variant="amber"
                disabled={mi.scanning || mi.committing}
                onClick={() => void mi.scan()}
                icon={mi.scanning ? <Loader2 className="h-4 w-4 animate-spin" /> : <FolderSearch className="h-4 w-4" />}
              >
                Scan
              </TapeDeckButton>
            </div>
          </div>
        )}

        {mi.scanning && (
          <div className="flex items-center justify-center gap-2 py-10 text-xs font-mono uppercase tracking-widest text-[var(--text-secondary)]">
            <Loader2 className="h-5 w-5 animate-spin text-[var(--accent-amber)]" /> Scanning files
          </div>
        )}

        {mi.scanError && (
          <div role="alert" className="p-3 rounded-[4px] border border-[var(--status-error)]/50 bg-[var(--status-error)]/10 text-xs font-mono text-[var(--status-error)]">
            {mi.scanError}
          </div>
        )}

        {!mi.scanning && !mi.scanError && mi.scanned && mi.rows.length === 0 && (
          <p className="py-10 text-center text-xs font-mono text-[var(--text-muted)]">No audio files found</p>
        )}

        {mi.rows.length > 0 && (
          <>
            <div className="flex flex-wrap items-center gap-1.5">
              <TapeDeckButton size="sm" disabled={busy || mi.identifyingAll} onClick={() => void mi.identifyAllUnmatched()}>
                {mi.identifyingAll ? 'Identifying…' : 'Identify all unmatched'}
              </TapeDeckButton>
              <TapeDeckButton size="sm" disabled={busy} onClick={() => mi.selectAll(true)}>
                Select all
              </TapeDeckButton>
              <TapeDeckButton size="sm" disabled={busy} onClick={() => mi.selectAll(false)}>
                Select none
              </TapeDeckButton>
              <span className="ml-auto text-[11px] font-mono text-[var(--text-muted)]">{mi.rows.length} files</span>
            </div>
            <ul className="space-y-2">
              {mi.rows.map((row) => (
                <ManualImportRowView
                  key={row.item.file_path}
                  row={row}
                  duplicate={mi.duplicateKeys.has(row.item.file_path)}
                  disabled={busy}
                  onToggle={mi.toggleRow}
                  onSelectTrack={mi.selectTrack}
                  onChangeAlbum={mi.changeAlbum}
                  onIdentify={mi.identifyRow}
                />
              ))}
            </ul>
          </>
        )}

        {mi.commitError && (
          <div role="alert" className="p-3 rounded-[4px] border border-[var(--status-error)]/50 bg-[var(--status-error)]/10 text-xs font-mono text-[var(--status-error)]">
            {mi.commitError}
          </div>
        )}
        {mi.downloadCleared && (
          <p className="text-xs font-mono text-[var(--status-success)]">The download was fully imported and cleared from the queue.</p>
        )}
      </div>
    </ObsidianModal>
  );
};
