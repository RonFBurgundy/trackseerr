import React from 'react';
import { AlertTriangle, Inbox, Loader2 } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';

export interface ListStatesProps {
  total: number;
  loading: boolean;
  error: string | null;
  emptyMessage: string;
  emptyHint?: string;
  /** Full reload; the retry for the whole-list error state. */
  onReload: () => void;
}

/** Initial-load skeleton, whole-list error and empty state shared by FlatList and VirtualGrid. Null when rows exist. */
export const ListStates: React.FC<ListStatesProps> = ({ total, loading, error, emptyMessage, emptyHint, onReload }) => {
  if (total > 0) return null;
  if (error !== null && !loading) {
    return (
      <div role="alert" className="p-6 flex flex-col items-center gap-3 text-center">
        <AlertTriangle className="h-6 w-6 text-red-400" />
        <p className="text-xs font-mono text-red-300 break-words max-w-md">{error}</p>
        <TapeDeckButton size="sm" onClick={onReload}>
          Retry
        </TapeDeckButton>
      </div>
    );
  }
  if (loading) {
    return (
      <div role="status" className="p-3 space-y-2">
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <div key={i} className="h-10 rounded-[3px] bg-[#1a1a1a] animate-pulse" />
        ))}
        <div className="flex items-center justify-center gap-2 pt-1 text-[11px] font-mono text-neutral-400 uppercase">
          <Loader2 className="h-3.5 w-3.5 animate-spin text-[#e5a00d]" /> Loading
        </div>
      </div>
    );
  }
  return (
    <div className="p-8 flex flex-col items-center gap-2 text-center">
      <Inbox className="h-6 w-6 text-neutral-600" />
      <p className="text-xs font-mono text-neutral-300">{emptyMessage}</p>
      {emptyHint && <p className="text-[11px] font-mono text-neutral-500">{emptyHint}</p>}
    </div>
  );
};

/** Slim bar shown over a list that has rows but whose last page request failed. */
export const ListErrorBar: React.FC<{ error: string; onRetry: () => void }> = ({ error, onRetry }) => (
  <div
    role="alert"
    className="flex flex-wrap items-center justify-center gap-2 px-3 py-1.5 bg-[#1a0e0e] border-t border-red-900/60 text-[11px] font-mono text-red-300"
  >
    <AlertTriangle className="h-3.5 w-3.5" />
    <span className="break-words">{error}</span>
    <TapeDeckButton size="sm" onClick={onRetry}>
      Retry
    </TapeDeckButton>
  </div>
);
