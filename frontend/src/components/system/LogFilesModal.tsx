import React from 'react';
import { Download, Loader2, RefreshCw } from 'lucide-react';
import { ObsidianModal, StatusMessage, TapeDeckButton } from '@/components/ui';
import { formatBytes } from '@/components/lists';
import { useLogFiles } from '@/hooks/useLogFiles';
import { formatTimestamp } from './formatters';

export interface LogFilesModalProps {
  isOpen: boolean;
  onClose: () => void;
}

/** Lists the log files on disk (current, rotated and old-format backups); each one downloads on its own. */
export const LogFilesModal: React.FC<LogFilesModalProps> = React.memo(({ isOpen, onClose }) => {
  const { files, isLoading, error, downloadingName, refresh, download } = useLogFiles(isOpen);

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="Download Log"
      subtitle="Current and rotated log files"
      maxWidth="sm:max-w-2xl"
      footer={
        <>
          <TapeDeckButton
            size="sm"
            onClick={() => void refresh()}
            disabled={isLoading}
            icon={isLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          >
            Refresh
          </TapeDeckButton>
          <TapeDeckButton size="sm" onClick={onClose}>
            Close
          </TapeDeckButton>
        </>
      }
    >
      {error && <StatusMessage variant="error" className="mb-3">{error}</StatusMessage>}
      {!isLoading && !error && files.length === 0 && (
        <p className="py-8 text-center text-xs font-mono text-[var(--text-muted)]">No log files found.</p>
      )}
      <ul className="space-y-1.5" aria-label="Log files">
        {files.map((f) => (
          <li
            key={f.name}
            className="flex items-center gap-3 p-2.5 bg-[var(--bg-card)] border border-[var(--border-subtle)] rounded-[4px]"
          >
            <div className="min-w-0 flex-1 font-mono">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-white break-all">{f.name}</span>
                {f.end_at === null || f.end_at === undefined ? (
                  <span className="px-1.5 py-0.5 rounded-[2px] bg-emerald-950/50 border border-emerald-800/60 text-[10px] uppercase text-emerald-400">
                    current
                  </span>
                ) : null}
              </div>
              <div className="mt-0.5 text-[11px] text-[var(--text-secondary)]">
                {f.start_at ? formatTimestamp(f.start_at) : 'unknown start'} {' - '}
                {f.end_at ? formatTimestamp(f.end_at) : 'now'}
                <span className="text-[var(--text-muted)]"> | {formatBytes(f.size_bytes)}</span>
              </div>
            </div>
            <TapeDeckButton
              size="sm"
              onClick={() => void download(f.name)}
              disabled={downloadingName !== null}
              aria-label={`Download ${f.name}`}
              icon={
                downloadingName === f.name ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Download className="h-3.5 w-3.5" />
                )
              }
            />
          </li>
        ))}
      </ul>
    </ObsidianModal>
  );
});
LogFilesModal.displayName = 'LogFilesModal';
