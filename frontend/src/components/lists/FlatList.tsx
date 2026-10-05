import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowUp } from 'lucide-react';
import { useVirtualizer } from '@tanstack/react-virtual';
import type { ListSortDir } from '@/types/activity';
import type { ListKey, VirtualPagedList } from '@/hooks/useVirtualPagedList';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import { useFillViewportHeight } from '@/hooks/useFillViewportHeight';
import { TapeDeckButton } from '@/components/ui';
import { ListViewportContext, type ListViewport } from './ListViewportContext';
import { ListErrorBar, ListStates } from './ListStates';
import { cancelJumpsOnUserScroll, createScrollJumper } from './scrollJump';

export interface FlatListColumn<T> {
  key: string;
  label: string;
  /** Sortable columns send `key` as the server `sort_key`. */
  sortable?: boolean;
  /** CSS grid track for desktop, e.g. `'120px'` or `'minmax(0,1.5fr)'`. Default `minmax(0,1fr)`. */
  width?: string;
  render: (item: T) => React.ReactNode;
  /** Omitted from the mobile card. */
  hideOnMobile?: boolean;
  /**
   * Role in the compact mobile card (`mobileLayout="compact"`): `title` is the bold first line (default: the first
   * visible column), `sub` joins the muted second line, `meta` the small third line, `end` sits on the right beside
   * the row actions, `hide` drops the column. Ignored by the stacked layout.
   */
  mobile?: MobileRole;
  /** Shown only from the `xl` breakpoint up on desktop; the `lg` grid drops it to keep the core columns readable. */
  xlOnly?: boolean;
  align?: 'left' | 'right';
}

export type MobileRole = 'title' | 'sub' | 'meta' | 'end' | 'hide';

export type RowTone = 'warning' | 'error' | null;

export interface FlatListProps<T> {
  /** Memoize: row memoization compares this by reference. */
  columns: ReadonlyArray<FlatListColumn<T>>;
  /** The list state from `useVirtualPagedList`: total, loading, error, getItem, ensureRange, reload, ... */
  list: VirtualPagedList<T>;
  getKey: (item: T) => ListKey;
  sortKey: string;
  sortDir: ListSortDir;
  onSortChange: (key: string, dir: ListSortDir) => void;
  /** Selection (over loaded rows) is enabled when both are provided. */
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
  /** Right-side rail slot; mount a `ScrubberRail` here. It reads the list viewport from context. */
  rail?: React.ReactNode;
  /** Max height of the scrolling area (CSS length). Default: fill exactly the remaining viewport height. */
  maxHeight?: string;
  /**
   * Mobile row layout. `compact` (one title line, a muted line, optional small meta line, badges and actions on the
   * right; ~64-72px per row) or `stacked` (every column on its own labelled line). Default `stacked`.
   */
  mobileLayout?: 'compact' | 'stacked';
  /** Hide the column-sort chip row on mobile (when the panel already offers a sort control). Default false. */
  hideMobileSortBar?: boolean;
}

interface MobileCardLayout<T> {
  title: FlatListColumn<T>;
  sub: ReadonlyArray<FlatListColumn<T>>;
  meta: ReadonlyArray<FlatListColumn<T>>;
  end: ReadonlyArray<FlatListColumn<T>>;
}

/** Splits the columns into the compact card's lines. Null when no column can be the title. */
function buildMobileLayout<T>(columns: ReadonlyArray<FlatListColumn<T>>): MobileCardLayout<T> | null {
  const roleOf = (c: FlatListColumn<T>): MobileRole => c.mobile ?? (c.hideOnMobile ? 'hide' : 'sub');
  const visible = columns.filter((c) => roleOf(c) !== 'hide');
  const title = visible.find((c) => c.mobile === 'title') ?? visible.find((c) => c.mobile === undefined) ?? visible[0];
  if (!title) return null;
  const rest = visible.filter((c) => c !== title);
  return {
    title,
    sub: rest.filter((c) => roleOf(c) === 'sub' || roleOf(c) === 'title'),
    meta: rest.filter((c) => roleOf(c) === 'meta'),
    end: rest.filter((c) => roleOf(c) === 'end'),
  };
}

/** Compact card heights (px): fixed so a jump to any row lands exactly. */
const COMPACT_ROW_PX = 64;
const COMPACT_ROW_META_PX = 72;

const TONE_CLASS: Record<'warning' | 'error', string> = {
  warning: 'border-l-[#e5a00d]',
  error: 'border-l-red-500',
};

/** Used until the first measurement of the remaining viewport height. */
const FALLBACK_MAX_HEIGHT = 'clamp(360px, calc(100dvh - 240px), 1000px)';
/** Border of the list box (1px each side) that the scroll area sits inside. */
const BOX_BORDER_PX = 2;
const DESKTOP_ROW_ESTIMATE_PX = 44;
const OVERSCAN_ROWS = 8;

const ROW_GRID =
  'flex flex-col gap-1.5 lg:gap-3 lg:items-center lg:grid lg:[grid-template-columns:var(--cols)] xl:[grid-template-columns:var(--cols-xl)] px-3 py-2.5 border-b border-[#1c1c1c] border-l-2';

interface RowProps<T> {
  item: T;
  rowKey: ListKey;
  columns: ReadonlyArray<FlatListColumn<T>>;
  selectable: boolean;
  selected: boolean;
  onToggle: (key: ListKey) => void;
  rowActions?: (item: T) => React.ReactNode;
  tone: RowTone;
  /** Set (below `lg`, compact layout) to render the compact card instead of the grid/stacked row. */
  card: MobileCardLayout<T> | null;
}

const SEPARATOR = <span aria-hidden="true" className="shrink-0 text-neutral-700">&middot;</span>;

function CardLine<T>({ cols, item, className }: { cols: ReadonlyArray<FlatListColumn<T>>; item: T; className: string }) {
  if (cols.length === 0) return null;
  return (
    <div className={`flex items-center gap-1.5 min-w-0 overflow-hidden whitespace-nowrap ${className}`}>
      {cols.map((c, i) => (
        <React.Fragment key={c.key}>
          {i > 0 && SEPARATOR}
          <span className="min-w-0 truncate">{c.render(item)}</span>
        </React.Fragment>
      ))}
    </div>
  );
}

function CardInner<T>({
  item,
  rowKey,
  card,
  selectable,
  selected,
  onToggle,
  rowActions,
  tone,
}: RowProps<T> & { card: MobileCardLayout<T> }) {
  const rowUid = useId();
  const actions = rowActions ? rowActions(item) : null;
  const hasEnd = card.end.length > 0 || actions !== null;
  return (
    <div
      role="row"
      aria-selected={selectable ? selected : undefined}
      className={`flex items-center gap-2.5 px-3 border-b border-[#1c1c1c] border-l-2 hover:bg-[#1a1a1a] ${
        tone ? TONE_CLASS[tone] : 'border-l-transparent'
      } ${selected ? 'bg-[#e5a00d]/5' : 'bg-[#141414]'}`}
      style={{ minHeight: card.meta.length > 0 ? COMPACT_ROW_META_PX : COMPACT_ROW_PX }}
    >
      {selectable && (
        <div role="cell" className="shrink-0 flex items-center justify-center w-6 self-stretch">
          <input
            type="checkbox"
            id={`${rowUid}-select`}
            name="select-row"
            checked={selected}
            onChange={() => onToggle(rowKey)}
            aria-label="Select row"
            className="h-5 w-5 accent-[#e5a00d] cursor-pointer"
          />
        </div>
      )}
      <div role="cell" className="min-w-0 flex-1 py-2 font-mono">
        <div className="truncate text-sm leading-5 font-bold text-white">{card.title.render(item)}</div>
        <CardLine cols={card.sub} item={item} className="text-xs leading-4 text-neutral-400" />
        <CardLine cols={card.meta} item={item} className="text-[10px] leading-[14px] text-neutral-500" />
      </div>
      {hasEnd && (
        <div role="cell" className="shrink-0 max-w-[55%] flex flex-wrap items-center justify-end gap-1.5 text-xs font-mono">
          {card.end.map((c) => (
            <span key={c.key} className="min-w-0">
              {c.render(item)}
            </span>
          ))}
          {actions}
        </div>
      )}
    </div>
  );
}

function RowInner<T>(props: RowProps<T>) {
  const { item, rowKey, columns, selectable, selected, onToggle, rowActions, tone, card } = props;
  const rowUid = useId();
  if (card) return <CardInner {...props} card={card} />;
  return (
    <div
      role="row"
      aria-selected={selectable ? selected : undefined}
      className={`${ROW_GRID} hover:bg-[#1a1a1a] ${tone ? TONE_CLASS[tone] : 'border-l-transparent'} ${
        selected ? 'bg-[#e5a00d]/5' : 'bg-[#141414]'
      }`}
    >
      {selectable && (
        <div role="cell" className="flex items-center min-h-[44px] lg:min-h-0">
          <input
            type="checkbox"
            id={`${rowUid}-select`}
            name="select-row"
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

/** Skeleton for a row whose page has not arrived. Fixed height so it never fights the virtualizer's measurement. */
const PlaceholderRow: React.FC<{ height: number }> = React.memo(({ height }) => (
  <div
    role="row"
    aria-busy="true"
    className="flex items-center px-3 border-b border-[#1c1c1c] border-l-2 border-l-transparent bg-[#141414]"
    style={{ height }}
  >
    <div className="w-full space-y-2">
      <div className="h-2.5 w-2/5 rounded-[2px] bg-[#1d1d1d] animate-pulse" />
      <div className="h-2 w-3/5 rounded-[2px] bg-[#191919] animate-pulse lg:hidden" />
    </div>
  </div>
));
PlaceholderRow.displayName = 'PlaceholderRow';

/**
 * Flat, sortable, virtualized list over a sparsely paged source. Only the rows in (and just around) the viewport are
 * mounted; rows whose page has not arrived render as skeleton placeholders. Desktop (`lg`+) is a sticky-header grid;
 * below `lg` each row collapses to a card (compact two/three-line, or stacked) with a sort bar. The scrolling area
 * fills the remaining viewport height (or `maxHeight`); a `ScrubberRail` in the `rail` slot controls it through `ListViewportContext`.
 */
export function FlatList<T>({
  columns,
  list,
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
  maxHeight,
  mobileLayout = 'stacked',
  hideMobileSortBar = false,
}: FlatListProps<T>): React.ReactElement {
  const { total, loading, error, getItem, ensureRange, version, generation, reload, retry, bindScroller } = list;
  const selectable = selectedKeys !== undefined && onSelectedKeysChange !== undefined;
  const listUid = useId();
  const selectAllRef = useRef<HTMLInputElement | null>(null);
  const headerRef = useRef<HTMLDivElement | null>(null);
  const [scrollEl, setScrollEl] = useState<HTMLDivElement | null>(null);
  const [headerH, setHeaderH] = useState<number>(0);
  const isDesktop = useMediaQuery('(min-width: 1024px)');
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const fillHeight = useFillViewportHeight(wrapperRef);
  const jumperRef = useRef(createScrollJumper());

  const card = useMemo(
    (): MobileCardLayout<T> | null => (!isDesktop && mobileLayout === 'compact' ? buildMobileLayout(columns) : null),
    [isDesktop, mobileLayout, columns]
  );

  const estimate = useMemo((): number => {
    if (isDesktop) return DESKTOP_ROW_ESTIMATE_PX;
    if (card) return card.meta.length > 0 ? COMPACT_ROW_META_PX : COMPACT_ROW_PX;
    const mobileCols = columns.filter((c) => !c.hideOnMobile).length;
    return 22 + mobileCols * 24 + (rowActions ? 44 : 0) + (selectable ? 44 : 0);
  }, [isDesktop, card, columns, rowActions, selectable]);

  const virtualizer = useVirtualizer({
    count: total,
    getScrollElement: () => scrollEl,
    estimateSize: () => estimate,
    overscan: OVERSCAN_ROWS,
    scrollMargin: headerH,
    scrollPaddingStart: headerH,
  });

  useEffect(() => {
    virtualizer.measure();
  }, [virtualizer, estimate]);

  useEffect(() => {
    const el = headerRef.current;
    if (!el) return undefined;
    const update = (): void => setHeaderH(el.offsetHeight);
    update();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const scrollToOffset = useCallback(
    (index: number): void => {
      jumperRef.current.jump(() => virtualizer.scrollToIndex(index, { align: 'start' }));
    },
    [virtualizer]
  );

  useEffect(() => bindScroller(scrollToOffset), [bindScroller, scrollToOffset]);
  useEffect(() => {
    const jumper = jumperRef.current;
    return () => jumper.cancel();
  }, []);
  useEffect(() => {
    if (!scrollEl) return undefined;
    return cancelJumpsOnUserScroll(scrollEl, jumperRef.current);
  }, [scrollEl]);

  // A reset (sort/filter change, reload) goes back to the top.
  useEffect(() => {
    if (scrollEl) scrollEl.scrollTop = 0;
  }, [generation, scrollEl]);

  const virtualItems = virtualizer.getVirtualItems();
  const firstIndex = virtualItems.length > 0 ? virtualItems[0].index : -1;
  const lastIndex = virtualItems.length > 0 ? virtualItems[virtualItems.length - 1].index : -1;
  useEffect(() => {
    if (firstIndex >= 0) ensureRange(firstIndex, lastIndex);
  }, [firstIndex, lastIndex, total, version, ensureRange]);

  // `list` is rebuilt whenever its cache version changes, so this re-reads the loaded rows exactly then.
  const loaded = useMemo(() => list.getLoadedItems(), [list]);
  const selectedCount = selectedKeys ? loaded.reduce((n, it) => n + (selectedKeys.has(getKey(it)) ? 1 : 0), 0) : 0;
  const allSelected = loaded.length > 0 && selectedCount === loaded.length;

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = selectedCount > 0 && !allSelected;
  }, [selectedCount, allSelected]);

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
    onSelectedKeysChange(allSelected ? new Set() : new Set(loaded.map(getKey)));
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

  const sortIndicator = (col: FlatListColumn<T>): React.ReactNode => {
    if (!col.sortable || col.key !== sortKey) return null;
    return sortDir === 'asc' ? <ArrowUp className="h-3 w-3 text-[#e5a00d]" /> : <ArrowDown className="h-3 w-3 text-[#e5a00d]" />;
  };

  const viewportHeight = virtualizer.scrollRect?.height ?? 0;
  const scrollOffset = virtualizer.scrollOffset ?? 0;
  const contentHeight = virtualizer.getTotalSize() + headerH;
  const fits = viewportHeight === 0 || contentHeight <= viewportHeight + 1;
  const atEnd = !fits && scrollOffset + viewportHeight >= contentHeight - 2;
  // Row containing the first pixel below the sticky header (+1px): the row at, or just entering, the top edge.
  const topRow = virtualItems.find((vi) => vi.end > scrollOffset + headerH + 1);
  const topIndex = topRow ? topRow.index : 0;
  const overlayRail = useMediaQuery('(max-width: 1023px)');
  const viewport = useMemo<ListViewport>(
    () => ({ scrollElement: scrollEl, topIndex, atEnd, overlayRail, total, fits, scrollToOffset }),
    [scrollEl, topIndex, atEnd, overlayRail, total, fits, scrollToOffset]
  );

  const hasRows = total > 0;

  return (
    <ListViewportContext.Provider value={viewport}>
      <div ref={wrapperRef} className="relative flex items-stretch gap-1.5">
        <div className="flex-1 min-w-0 border border-[#222222] rounded-[4px] bg-[#141414] overflow-hidden" style={gridVars}>
          <div
            ref={setScrollEl}
            tabIndex={0}
            role="region"
            aria-label={`${ariaLabel} (scrollable)`}
            className="virtual-scroll relative"
            style={{ maxHeight: maxHeight ?? (fillHeight !== null ? fillHeight - BOX_BORDER_PX : FALLBACK_MAX_HEIGHT) }}
          >
            <div ref={headerRef} className="sticky top-0 z-10">
              {/* Desktop header */}
              <div
                role="row"
                className="hidden lg:grid lg:[grid-template-columns:var(--cols)] xl:[grid-template-columns:var(--cols-xl)] gap-3 items-center px-3 py-2 bg-[#121212] border-b border-[#2a2a2a] text-[10px] uppercase tracking-wider text-neutral-400 font-mono"
              >
                {selectable && (
                  <div role="columnheader">
                    <input
                      ref={selectAllRef}
                      id={`${listUid}-select-all-desktop`}
                      name="select-all-loaded"
                      type="checkbox"
                      checked={allSelected}
                      onChange={toggleAll}
                      disabled={loaded.length === 0}
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
              {(!hideMobileSortBar || selectable) && (
              <div className="lg:hidden flex items-center gap-1.5 overflow-x-auto px-2 py-1.5 bg-[#121212] border-b border-[#2a2a2a]">
                {selectable && (
                  <label className="flex items-center gap-1.5 text-[10px] uppercase font-mono text-neutral-400 shrink-0 min-h-[44px] pr-2">
                    <input
                      id={`${listUid}-select-all-mobile`}
                      name="select-all-loaded"
                      type="checkbox"
                      checked={allSelected}
                      onChange={toggleAll}
                      disabled={loaded.length === 0}
                      aria-label="Select all loaded rows"
                      className="h-4 w-4 accent-[#e5a00d]"
                    />
                    All
                  </label>
                )}
                {!hideMobileSortBar &&
                  columns
                  .filter((c) => c.sortable)
                  .map((col) => (
                    <TapeDeckButton
                      key={col.key}
                      size="sm"
                      active={col.key === sortKey}
                      onClick={() => handleSort(col)}
                      className="shrink-0 whitespace-nowrap"
                      aria-label={`Sort by ${col.label}`}
                      icon={sortIndicator(col)}
                    >
                      {col.label}
                    </TapeDeckButton>
                  ))}
              </div>
              )}
            </div>

            <ListStates
              total={total}
              loading={loading}
              error={error}
              emptyMessage={emptyMessage}
              emptyHint={emptyHint}
              onReload={reload}
            />

            {hasRows && (
              <div
                role="table"
                aria-label={ariaLabel}
                aria-busy={loading}
                aria-rowcount={total}
                className="relative w-full"
                style={{ height: virtualizer.getTotalSize() }}
              >
                {virtualItems.map((vi) => {
                  const item = getItem(vi.index);
                  const key = item !== undefined ? getKey(item) : vi.index;
                  return (
                    <div
                      key={vi.index}
                      data-index={vi.index}
                      ref={virtualizer.measureElement}
                      aria-rowindex={vi.index + 1}
                      className="absolute left-0 top-0 w-full"
                      style={{ transform: `translateY(${vi.start - headerH}px)` }}
                    >
                      {item !== undefined ? (
                        <Row<T>
                          item={item}
                          rowKey={key}
                          columns={columns}
                          selectable={selectable}
                          selected={selectedKeys?.has(key) ?? false}
                          onToggle={toggleKey}
                          rowActions={rowActions}
                          tone={rowTone ? rowTone(item) : null}
                          card={card}
                        />
                      ) : (
                        <PlaceholderRow height={estimate} />
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
          {error !== null && hasRows && <ListErrorBar error={error} onRetry={retry} />}
        </div>
        {rail && (
          <aside
            className={overlayRail ? 'absolute right-px bottom-px z-20' : 'shrink-0 self-stretch'}
            // Overlay: starts under the sticky header so it never covers the sort chips.
            style={overlayRail ? { top: headerH + 1 } : undefined}
          >
            {rail}
          </aside>
        )}
      </div>
    </ListViewportContext.Provider>
  );
}
