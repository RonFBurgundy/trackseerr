import React, { useCallback, useEffect, useRef } from 'react';
import { AlertTriangle, ArrowDown, ArrowUp, Loader2, Inbox } from 'lucide-react';
import type { ListSortDir } from '@/types/activity';
import type { ListKey } from '@/hooks/useInfiniteList';
import { TapeDeckButton } from '@/components/ui';

export interface FlatListColumn<T> {
  key: string;
  label: string;
  /** Sortable columns send `key` as the server `sort_key`. */
  sortable?: boolean;
  /** CSS grid track for desktop, e.g. `'120px'` or `'minmax(0,1.5fr)'`. Default `minmax(0,1fr)`. */
  width?: string;
  render: (item: T) => React.ReactNode;
  /** Omitted from the stacked mobile card. The first column is always the card title. */
  hideOnMobile?: boolean;
  /** Shown only from the `xl` breakpoint up on desktop; the `lg` grid drops it to keep the core columns readable. */
  xlOnly?: boolean;
  align?: 'left' | 'right';
}

export type RowTone = 'warning' | 'error' | null;

export interface FlatListProps<T> {
  /** Memoize: row memoization compares this by reference. */
  columns: ReadonlyArray<FlatListColumn<T>>;
  items: readonly T[];
  total: number;
  loading: boolean;
  error: string | null;
  hasMore: boolean;
  /** Appends the next page; also used to retry after an append error. */
  onLoadMore: () => void;
  /** Reloads from page 1; used by the full-page error state. */
  onReload: () => void;
  getKey: (item: T) => ListKey;
  sortKey: string;
  sortDir: ListSortDir;
  onSortChange: (key: string, dir: ListSortDir) => void;
  /** Selection is enabled when both are provided. */
  selectedKeys?: ReadonlySet<ListKey>;
  onSelectedKeysChange?: (next: ReadonlySet<ListKey>) => void;
  /** Memoize with useCallback. */
  rowActions?: (item: T) => React.ReactNode;
  actionsLabel?: string;
  actionsWidth?: string;
  /** Left-edge colour flag for a row (e.g. stalled). */
  rowTone?: (item: T) => RowTone;
  emptyMessage: string;
  emptyHint?: string;
  ariaLabel: string;
  /**
   * Right-side rail slot. Phase 4 mounts the alphabet/group scrubber here; it is rendered in a sticky
   * column beside the rows and sized by its own content.
   */
  rail?: React.ReactNode;
}

const TONE_CLASS: Record<'warning' | 'error', string> = {
  warning: 'border-l-[#e5a00d]',
  error: 'border-l-red-500',
};

interface RowProps<T> {
  item: T;
  rowKey: ListKey;
  columns: ReadonlyArray<FlatListColumn<T>>;
  selectable: boolean;
  selected: boolean;
  onToggle: (key: ListKey) => void;
  rowActions?: (item: T) => React.ReactNode;
  tone: RowTone;
}

function RowInner<T>({ item, rowKey, columns, selectable, selected, onToggle, rowActions, tone }: RowProps<T>) {
  return (
    <div
      role="row"
      aria-selected={selectable ? selected : undefined}
      className={`flex flex-col gap-1.5 lg:gap-3 lg:items-center lg:grid lg:[grid-template-columns:var(--cols)] xl:[grid-template-columns:var(--cols-xl)] px-3 py-2.5 border-b border-[#1c1c1c] border-l-2 hover:bg-[#1a1a1a] [content-visibility:auto] [contain-intrinsic-size:auto_52px] ${
        tone ? TONE_CLASS[tone] : 'border-l-transparent'
      } ${selected ? 'bg-[#e5a00d]/5' : 'bg-[#141414]'}`}
    >
      {selectable && (
        <div role="cell" className="flex items-center min-h-[44px] lg:min-h-0">
          <input
            type="checkbox"
            checked={selected}
            onChange={() => onToggle(rowKey)}
            aria-label="Select row"
            className="h-4 w-4 accent-[#e5a00d] cursor-pointer"
          />
        </div>
      )}
      {columns.map((col, idx) => (
        <div
          key={col.key}
          role="cell"
          className={`min-w-0 text-xs font-mono ${
            col.hideOnMobile
              ? col.xlOnly
                ? 'hidden xl:block'
                : 'hidden lg:block'
              : col.xlOnly
                ? 'flex lg:hidden xl:block justify-between gap-3'
                : 'flex lg:block justify-between gap-3'
          } ${col.align === 'right' ? 'lg:text-right' : ''} ${
            idx === 0 ? 'text-white font-bold text-sm lg:text-xs' : 'text-neutral-300'
          }`}
        >
          {idx !== 0 && (
            <span className="lg:hidden text-[10px] uppercase tracking-wider text-neutral-500 shrink-0">
              {col.label}
            </span>
          )}
          <div className="min-w-0 break-words">{col.render(item)}</div>
        </div>
      ))}
      {rowActions && (
        <div role="cell" className="flex flex-wrap items-center gap-1.5 lg:justify-end pt-1 lg:pt-0">
          {rowActions(item)}
        </div>
      )}
    </div>
  );
}

const Row = React.memo(RowInner) as typeof RowInner;

/** How far below the viewport the sentinel may sit and still trigger the next page. */
const SENTINEL_MARGIN_PX = 400;

function isWithinPreloadMargin(el: HTMLElement, margin: number): boolean {
  const rect = el.getBoundingClientRect();
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
  return rect.top <= viewportHeight + margin && rect.bottom >= -margin;
}

/**
 * Flat, sortable, lazily-paged list. Desktop (`lg`+) is a sticky-header grid; below `lg` each row collapses to a
 * stacked card with a compact sort bar. Rows are memoized and use `content-visibility: auto`, so appending
 * a page does not re-render existing rows and off-screen rows skip layout/paint.
 */
export function FlatList<T>({
  columns,
  items,
  total,
  loading,
  error,
  hasMore,
  onLoadMore,
  onReload,
  getKey,
  sortKey,
  sortDir,
  onSortChange,
  selectedKeys,
  onSelectedKeysChange,
  rowActions,
  actionsLabel = 'Actions',
  actionsWidth = '170px',
  rowTone,
  emptyMessage,
  emptyHint,
  ariaLabel,
  rail,
}: FlatListProps<T>): React.ReactElement {
  const selectable = selectedKeys !== undefined && onSelectedKeysChange !== undefined;
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const selectAllRef = useRef<HTMLInputElement | null>(null);

  const selectedCount = selectedKeys ? items.reduce((n, it) => n + (selectedKeys.has(getKey(it)) ? 1 : 0), 0) : 0;
  const allSelected = items.length > 0 && selectedCount === items.length;

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = selectedCount > 0 && !allSelected;
  }, [selectedCount, allSelected]);

  const canObserve = typeof IntersectionObserver !== 'undefined';
  const sentinelActive = hasMore && !loading && !error;
  useEffect(() => {
    const el = sentinelRef.current;
    if (!canObserve || !el || !sentinelActive) return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) onLoadMore();
      },
      { rootMargin: `${SENTINEL_MARGIN_PX}px 0px` }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [canObserve, sentinelActive, onLoadMore, items.length]);

  // An observer only reports changes, so when a load (or a background refresh) finishes while the sentinel is
  // already inside the preload margin nothing would fire. Re-check geometry after every load so paging continues
  // without a user scroll; `onLoadMore` is a no-op while a page is in flight.
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || !sentinelActive) return;
    if (isWithinPreloadMargin(el, SENTINEL_MARGIN_PX)) onLoadMore();
  }, [sentinelActive, onLoadMore, items.length, total]);

  const toggleKey = useCallback(
    (key: ListKey) => {
      if (!selectedKeys || !onSelectedKeysChange) return;
      const next = new Set(selectedKeys);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      onSelectedKeysChange(next);
    },
    [selectedKeys, onSelectedKeysChange]
  );

  const toggleAll = (): void => {
    if (!onSelectedKeysChange) return;
    onSelectedKeysChange(allSelected ? new Set() : new Set(items.map(getKey)));
  };

  const handleSort = (col: FlatListColumn<T>): void => {
    if (!col.sortable) return;
    onSortChange(col.key, col.key === sortKey ? (sortDir === 'asc' ? 'desc' : 'asc') : 'asc');
  };

  const buildTemplate = (cols: ReadonlyArray<FlatListColumn<T>>): string =>
    [
      selectable ? '28px' : null,
      ...cols.map((c) => c.width ?? 'minmax(0,1fr)'),
      rowActions ? actionsWidth : null,
    ]
      .filter((t): t is string => t !== null)
      .join(' ');
  const gridVars = {
    '--cols': buildTemplate(columns.filter((c) => !c.xlOnly)),
    '--cols-xl': buildTemplate(columns),
  } as React.CSSProperties;

  const initialLoading = loading && items.length === 0;
  const fatalError = error !== null && items.length === 0 && !loading;
  const empty = !loading && error === null && items.length === 0;

  const sortIndicator = (col: FlatListColumn<T>): React.ReactNode => {
    if (!col.sortable || col.key !== sortKey) return null;
    return sortDir === 'asc' ? <ArrowUp className="h-3 w-3 text-[#e5a00d]" /> : <ArrowDown className="h-3 w-3 text-[#e5a00d]" />;
  };

  return (
    <div className="flex items-start gap-2">
      <div className="flex-1 min-w-0 border border-[#222222] rounded-[4px] bg-[#141414]" style={gridVars}>
        {/* Desktop sticky header */}
        <div
          role="row"
          className="hidden lg:grid lg:[grid-template-columns:var(--cols)] xl:[grid-template-columns:var(--cols-xl)] gap-3 items-center px-3 py-2 sticky top-0 z-10 bg-[#121212] border-b border-[#2a2a2a] rounded-t-[4px] text-[10px] uppercase tracking-wider text-neutral-400 font-mono"
        >
          {selectable && (
            <div role="columnheader">
              <input
                ref={selectAllRef}
                type="checkbox"
                checked={allSelected}
                onChange={toggleAll}
                disabled={items.length === 0}
                aria-label="Select all loaded rows"
                className="h-4 w-4 accent-[#e5a00d] cursor-pointer"
              />
            </div>
          )}
          {columns.map((col) => (
            <div
              key={col.key}
              role="columnheader"
              aria-sort={
                col.sortable && col.key === sortKey ? (sortDir === 'asc' ? 'ascending' : 'descending') : undefined
              }
              className={`${col.xlOnly ? 'hidden xl:block ' : ''}${col.align === 'right' ? 'text-right' : ''}`}
            >
              {col.sortable ? (
                <button
                  type="button"
                  onClick={() => handleSort(col)}
                  className={`inline-flex items-center gap-1 uppercase tracking-wider hover:text-white ${
                    col.key === sortKey ? 'text-white' : ''
                  }`}
                >
                  {col.label}
                  {sortIndicator(col)}
                </button>
              ) : (
                col.label
              )}
            </div>
          ))}
          {rowActions && <div role="columnheader" className="text-right">{actionsLabel}</div>}
        </div>

        {/* Mobile sort bar */}
        <div className="lg:hidden sticky top-0 z-10 flex items-center gap-1.5 overflow-x-auto px-2 py-1.5 bg-[#121212] border-b border-[#2a2a2a] rounded-t-[4px]">
          {selectable && (
            <label className="flex items-center gap-1.5 text-[10px] uppercase font-mono text-neutral-400 shrink-0 min-h-[44px] pr-2">
              <input
                type="checkbox"
                checked={allSelected}
                onChange={toggleAll}
                disabled={items.length === 0}
                aria-label="Select all loaded rows"
                className="h-4 w-4 accent-[#e5a00d]"
              />
              All
            </label>
          )}
          {columns
            .filter((c) => c.sortable)
            .map((col) => (
              <TapeDeckButton
                key={col.key}
                size="sm"
                active={col.key === sortKey}
                onClick={() => handleSort(col)}
                className="shrink-0"
                aria-label={`Sort by ${col.label}`}
              >
                {col.label}
                {sortIndicator(col)}
              </TapeDeckButton>
            ))}
        </div>

        <div role="table" aria-label={ariaLabel} aria-busy={loading} aria-rowcount={total}>
          {initialLoading && (
            <div role="status" className="p-3 space-y-2">
              {[0, 1, 2, 3].map((i) => (
                <div key={i} className="h-10 rounded-[3px] bg-[#1a1a1a] animate-pulse" />
              ))}
              <div className="flex items-center justify-center gap-2 pt-1 text-[11px] font-mono text-neutral-400 uppercase">
                <Loader2 className="h-3.5 w-3.5 animate-spin text-[#e5a00d]" /> Loading
              </div>
            </div>
          )}

          {fatalError && (
            <div role="alert" className="p-6 flex flex-col items-center gap-3 text-center">
              <AlertTriangle className="h-6 w-6 text-red-400" />
              <p className="text-xs font-mono text-red-300 break-words max-w-md">{error}</p>
              <TapeDeckButton size="sm" onClick={onReload}>
                Retry
              </TapeDeckButton>
            </div>
          )}

          {empty && (
            <div className="p-8 flex flex-col items-center gap-2 text-center">
              <Inbox className="h-6 w-6 text-neutral-600" />
              <p className="text-xs font-mono text-neutral-300">{emptyMessage}</p>
              {emptyHint && <p className="text-[11px] font-mono text-neutral-500">{emptyHint}</p>}
            </div>
          )}

          {items.map((item) => {
            const key = getKey(item);
            return (
              <Row<T>
                key={key}
                item={item}
                rowKey={key}
                columns={columns}
                selectable={selectable}
                selected={selectedKeys?.has(key) ?? false}
                onToggle={toggleKey}
                rowActions={rowActions}
                tone={rowTone ? rowTone(item) : null}
              />
            );
          })}
        </div>

        {items.length > 0 && (
          <div className="px-3 py-3 text-center text-[11px] font-mono text-neutral-500">
            {loading && (
              <span role="status" className="inline-flex items-center gap-2 text-neutral-400">
                <Loader2 className="h-3.5 w-3.5 animate-spin text-[#e5a00d]" /> Loading more
              </span>
            )}
            {error !== null && !loading && (
              <span role="alert" className="inline-flex flex-wrap items-center justify-center gap-2 text-red-300">
                <AlertTriangle className="h-3.5 w-3.5" />
                <span className="break-words">{error}</span>
                <TapeDeckButton size="sm" onClick={onLoadMore}>
                  Retry
                </TapeDeckButton>
              </span>
            )}
            {!loading && error === null && hasMore && !canObserve && (
              <TapeDeckButton size="sm" onClick={onLoadMore}>
                Load more
              </TapeDeckButton>
            )}
            {!loading && error === null && !hasMore && (
              <span className="uppercase tracking-wider">End of list - {total} {total === 1 ? 'item' : 'items'}</span>
            )}
          </div>
        )}
        <div ref={sentinelRef} aria-hidden="true" className="h-px" />
      </div>
      {rail && <aside className="sticky top-2 self-start shrink-0">{rail}</aside>}
    </div>
  );
}
