import React, { useState } from 'react';
import { ArrowRight, History, Loader2, RotateCcw } from 'lucide-react';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import type { MatchOverride } from '@/services/missingService';
import { errorMessage } from '@/services/apiClient';

export interface MatchOverridesModalProps {
  isOpen: boolean;
  onClose: () => void;
  overrides: MatchOverride[];
  loading: boolean;
  onDelete: (id: number) => Promise<void>;
  onReload: () => Promise<void>;
}

export const MatchOverridesModal: React.FC<MatchOverridesModalProps> = ({
  isOpen,
  onClose,
  overrides,
  loading,
  onDelete,
  onReload,
}) => {
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const handleDelete = async (id: number) => {
    setDeletingId(id);
    setActionError(null);
    try {
      await onDelete(id);
    } catch (err: unknown) {
      setActionError(errorMessage(err, 'Failed to undo match override'));
    } finally {
      setDeletingId(null);
    }
  };

  const footer = <TapeDeckButton onClick={onClose}>Close</TapeDeckButton>;

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="Match Memory Overrides"
      subtitle="Manual track corrections remembered by the system"
      maxWidth="sm:max-w-3xl"
      footer={footer}
    >
      <div className="space-y-4">
        {actionError && (
          <div
            role="alert"
            className="p-3 rounded-[3px] border border-[var(--status-error)]/50 bg-[var(--status-error)]/10 text-xs font-mono text-[var(--status-error)]"
          >
            {actionError}
          </div>
        )}

        {loading ? (
          <div className="flex flex-col items-center justify-center gap-2 py-12 text-xs font-mono uppercase tracking-widest text-[var(--text-secondary)]">
            <Loader2 className="h-5 w-5 animate-spin text-[var(--accent-amber)]" />
            <span>Loading match overrides...</span>
          </div>
        ) : overrides.length === 0 ? (
          <div className="py-12 text-center space-y-2">
            <History className="h-8 w-8 text-[var(--text-muted)] mx-auto opacity-70" />
            <p className="text-sm font-medium text-white">No Match Memory Overrides</p>
            <p className="text-xs text-neutral-400 font-mono">
              When you manually match missing playlist tracks to library tracks, the corrections appear here.
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs font-mono text-neutral-400 px-1">
              <span>{overrides.length} stored override{overrides.length === 1 ? '' : 's'}</span>
              <button
                type="button"
                onClick={() => void onReload()}
                className="text-neutral-400 hover:text-white transition-colors"
              >
                Refresh
              </button>
            </div>

            <div
              tabIndex={0}
              role="region"
              aria-label="Match overrides list (scrollable)"
              className="virtual-scroll overflow-y-auto border border-[#222222] rounded-[4px] bg-[#0d0d0d] divide-y divide-[#1c1c1c] max-h-[60vh]"
            >
              {overrides.map((override) => {
                const isDeleting = deletingId === override.id;

                return (
                  <div
                    key={override.id}
                    className="p-3 flex items-center justify-between gap-3 hover:bg-[#141414] transition-colors"
                  >
                    <div className="min-w-0 flex-1 space-y-1 font-mono text-xs">
                      <div className="flex items-center gap-1.5 text-neutral-400 break-all">
                        <span className="shrink-0 text-[10px] uppercase text-neutral-500 font-bold">Playlist Source:</span>
                        <span className="text-neutral-200">
                          {override.source_artist} - {override.source_title}
                        </span>
                      </div>
                      <div className="flex items-center gap-1.5 text-[#e5a00d] break-all">
                        <ArrowRight className="h-3 w-3 shrink-0 text-[#e5a00d]/70" />
                        <span className="shrink-0 text-[10px] uppercase text-[#e5a00d]/80 font-bold">Matched Library Track:</span>
                        <span className="font-semibold text-white">
                          {override.plex_artist} - {override.plex_title}
                        </span>
                      </div>
                      {override.created_at && (
                        <div className="text-[10px] text-neutral-500">
                          Matched on {new Date(override.created_at).toLocaleString()}
                        </div>
                      )}
                    </div>

                    <div className="shrink-0">
                      <TapeDeckButton
                        size="sm"
                        variant="danger"
                        disabled={isDeleting}
                        onClick={() => void handleDelete(override.id)}
                        icon={isDeleting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
                        title="Undo match override"
                        aria-label={`Undo match override for ${override.source_title}`}
                      >
                        Undo
                      </TapeDeckButton>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </ObsidianModal>
  );
};
