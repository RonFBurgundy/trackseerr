import React, { useLayoutEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { CornerDownLeft } from 'lucide-react';
import type { LibrarySearchItem } from '@/hooks/useLibrarySearch';
import type { LibrarySearchResponse } from '@/types/models';

export interface LibrarySearchResultsProps {
  results: LibrarySearchResponse | null;
  items: LibrarySearchItem[];
  loading: boolean;
  error: string | null;
  activeIndex: number;
  listId: string;
  /** The element the panel hangs under; the panel matches its left edge and width. */
  anchor: HTMLElement | null;
  /** The element whose left edge and width the panel spans (defaults to `anchor`). */
  widthAnchor?: HTMLElement | null;
  onHover: (index: number) => void;
  onPickArtist: (id: string) => void;
  onPickAlbum: (id: string) => void;
  onPickTrack: (albumId: string) => void;
  onPickSuggestion: (text: string) => void;
}

interface PanelRect {
  top: number;
  left: number;
  width: number;
}

const SECTIONS: { kind: LibrarySearchItem['kind']; label: string }[] = [
  { kind: 'artist', label: 'Artists' },
  { kind: 'album', label: 'Albums' },
  { kind: 'track', label: 'Tracks' },
];

function primaryText(item: LibrarySearchItem): string {
  if (item.kind === 'suggestion') return `Did you mean ${item.text}?`;
  if (item.kind === 'artist') return item.name;
  return item.title;
}

function secondaryText(item: LibrarySearchItem): string {
  if (item.kind === 'artist') return `${item.albumCount} ${item.albumCount === 1 ? 'album' : 'albums'}`;
  if (item.kind === 'album') return item.year ? `${item.artistName} · ${item.year}` : item.artistName;
  if (item.kind === 'track') return `${item.artistName} · ${item.albumTitle}`;
  return '';
}

/** Library-wide matches under the search box. Portaled because the toolbar row clips overflow. */
export const LibrarySearchResults: React.FC<LibrarySearchResultsProps> = ({
  results,
  items,
  loading,
  error,
  activeIndex,
  listId,
  anchor,
  widthAnchor,
  onHover,
  onPickArtist,
  onPickAlbum,
  onPickTrack,
  onPickSuggestion,
}) => {
  const [rect, setRect] = useState<PanelRect | null>(null);

  useLayoutEffect(() => {
    if (!anchor) return undefined;
    const measure = (): void => {
      const top = anchor.getBoundingClientRect().bottom + 4;
      const r = (widthAnchor ?? anchor).getBoundingClientRect();
      const left = Math.max(8, r.left);
      const width = Math.min(r.width, window.innerWidth - left - 8);
      setRect({ top, left, width });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(anchor);
    if (widthAnchor && widthAnchor !== anchor) {
      ro.observe(widthAnchor);
    }
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      ro.disconnect();
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, [anchor, widthAnchor]);

  if (!anchor || !rect) return null;

  const pick = (item: LibrarySearchItem): void => {
    if (item.kind === 'suggestion') onPickSuggestion(item.text);
    else if (item.kind === 'artist') onPickArtist(item.id);
    else if (item.kind === 'album') onPickAlbum(item.id);
    else onPickTrack(item.albumId);
  };

  const renderRow = (item: LibrarySearchItem): React.ReactNode => {
    const i = items.indexOf(item);
    const isActive = i === activeIndex;
    const secondary = secondaryText(item);
    return (
      <li
        key={`${item.kind}-${item.kind === 'suggestion' ? item.text : item.id}`}
        id={`${listId}-${i}`}
        role="option"
        aria-selected={isActive}
        onMouseEnter={() => onHover(i)}
        onClick={() => pick(item)}
        className={`flex min-h-[40px] cursor-pointer items-center gap-2 rounded-[3px] border px-2.5 py-1 ${
          isActive ? 'border-[var(--accent-amber)]/50 bg-[#151515]' : 'border-[#1e1e1e] bg-[#121212]'
        }`}
      >
        <div className="flex min-w-0 flex-1 items-baseline gap-2">
          <span
            className={`truncate text-[13px] font-mono ${item.kind === 'suggestion' ? 'text-[var(--accent-amber)]' : 'font-bold text-white'}`}
          >
            {primaryText(item)}
          </span>
          {secondary && <span className="truncate text-[10px] font-mono text-[var(--text-secondary)]">{secondary}</span>}
        </div>
        {isActive && <CornerDownLeft className="h-3.5 w-3.5 shrink-0 text-[var(--accent-amber)]" aria-hidden="true" />}
      </li>
    );
  };

  const suggestion = items.find((it) => it.kind === 'suggestion');
  const noMatches = !loading && !error && results !== null && items.length === 0;

  return createPortal(
    <div
      style={{ top: rect.top, left: rect.left, width: rect.width }}
      // Keep focus in the input while the panel is pressed.
      onMouseDown={(e) => e.preventDefault()}
      className="fixed z-40 max-h-[60dvh] overflow-y-auto overscroll-y-contain rounded-[4px] border border-[#1f1f1f] bg-[#0d0d0d] p-1.5 shadow-transport-bay"
    >
      <ul id={listId} role="listbox" aria-label="Library search results" className="flex flex-col gap-0.5">
        {suggestion && renderRow(suggestion)}
        {SECTIONS.map(({ kind, label }) => {
          const rows = items.filter((it) => it.kind === kind);
          if (rows.length === 0) return null;
          return (
            <React.Fragment key={kind}>
              <li role="presentation" className="px-1 pt-1.5 text-[10px] font-mono uppercase tracking-wider text-[var(--text-muted)]">
                {label}
              </li>
              {rows.map(renderRow)}
            </React.Fragment>
          );
        })}
        {error && <li role="presentation" className="px-2.5 py-2 text-[12px] font-mono text-[var(--status-error)]">{error}</li>}
        {loading && items.length === 0 && (
          <li role="presentation" className="px-2.5 py-2 text-[12px] font-mono text-[var(--text-muted)]">Searching...</li>
        )}
        {noMatches && (
          <li role="presentation" className="px-2.5 py-2 text-[12px] font-mono text-[var(--text-muted)]">No matches in your library</li>
        )}
      </ul>
    </div>,
    document.body
  );
};
