import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import type { ListKey, VirtualPagedList } from '@/hooks/useVirtualPagedList';
import { useFillViewportHeight } from '@/hooks/useFillViewportHeight';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import { ListViewportContext, type ListViewport } from './ListViewportContext';
import { ListErrorBar, ListStates } from './ListStates';
import { cancelJumpsOnUserScroll, createScrollJumper } from './scrollJump';

export interface VirtualGridProps<T> {
  /** The list state from `useVirtualPagedList`. */
  list: VirtualPagedList<T>;
  getKey: (item: T) => ListKey;
  /** Renders one loaded tile. It fills the tile box; keep it memoized-friendly (stable callback). */
  renderItem: (item: T, index: number) => React.ReactNode;
  /** Renders a tile whose page has not arrived. Default: a square skeleton with two caption bars. */
  renderPlaceholder?: (index: number) => React.ReactNode;
  /** Narrowest a tile may get (px); the column count is the most that fit the container width. Default 150. */
  minTileWidth?: number;
  /** Space between tiles (px). Default 12. */
  gap?: number;
  /** Exact height of whatever sits under the square cover in a tile (caption, borders), px. Default 48. */
  captionHeight?: number;
  emptyMessage: string;
  emptyHint?: string;
  ariaLabel: string;
  /** Right-side rail slot; mount a `ScrubberRail` here. */
  rail?: React.ReactNode;
  /** Max height of the scrolling area (CSS length). Default: fill exactly the remaining viewport height. */
  maxHeight?: string;
}

/** Side padding inside the scroll area (px). */
const PAD = 12;
/** Used until the first measurement of the remaining viewport height. */
const FALLBACK_MAX_HEIGHT = 'clamp(360px, calc(100dvh - 240px), 1000px)';
/** Border of the list box (1px each side) that the scroll area sits inside. */
const BOX_BORDER_PX = 2;
const DEFAULT_PLACEHOLDER = (): React.ReactNode => (
  <div className="w-full space-y-2" aria-hidden="true">
    <div className="aspect-square w-full rounded-[4px] bg-[#1a1a1a] animate-pulse" />
    <div className="h-2.5 w-4/5 rounded-[2px] bg-[#1d1d1d] animate-pulse" />
    <div className="h-2 w-3/5 rounded-[2px] bg-[#191919] animate-pulse" />
  </div>
);

/**
 * Virtualized cover grid over a sparsely paged source. The column count follows the container width; each virtual
 * row holds N tiles, so a 5,000-item library mounts a few dozen tiles. Unloaded tiles render placeholders. Rows have an exact,
 * computed height (square cover from the container width plus the caption), never measured, so a jump to any row
 * lands precisely. Shares the `rail` slot and `ListViewportContext` contract with FlatList, so the same ScrubberRail works on both.
 */
export function VirtualGrid<T>({
  list,
  getKey,
  renderItem,
  renderPlaceholder,
  minTileWidth = 150,
  gap = 12,
  captionHeight = 48,
  emptyMessage,
  emptyHint,
  ariaLabel,
  rail,
  maxHeight,
}: VirtualGridProps<T>): React.ReactElement {
  const { total, loading, error, getItem, ensureRange, version, generation, reload, retry, bindScroller } = list;
  const [scrollEl, setScrollEl] = useState<HTMLDivElement | null>(null);
  const [width, setWidth] = useState<number>(0);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const fillHeight = useFillViewportHeight(wrapperRef);
  const overlayRail = useMediaQuery('(max-width: 1023px)');
  const jumperRef = useRef(createScrollJumper());

  useEffect(() => {
    if (!scrollEl) return undefined;
    const update = (): void => {
      const cs = getComputedStyle(scrollEl);
      const inner = scrollEl.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
      setWidth(inner - PAD * 2);
    };
    update();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(update);
    observer.observe(scrollEl);
    return () => observer.disconnect();
  }, [scrollEl]);

  const columns = Math.max(1, Math.floor((width + gap) / (minTileWidth + gap)));
  const tileWidth = width > 0 ? (width - gap * (columns - 1)) / columns : minTileWidth;
  const rowCount = Math.ceil(total / columns);
  // Integer, so the laid-out height and the virtualizer's size are the same number (no drift over thousands of rows).
  const rowHeight = Math.ceil(tileWidth) + captionHeight;

  const virtualizer = useVirtualizer({
    count: rowCount,
    getScrollElement: () => scrollEl,
    estimateSize: () => rowHeight,
    overscan: 3,
    gap,
    paddingStart: PAD,
    paddingEnd: PAD,
  });

  useEffect(() => {
    virtualizer.measure();
  }, [virtualizer, rowHeight, columns]);

  const scrollToOffset = useCallback(
    (index: number): void => {
      const row = Math.floor(index / columns);
      jumperRef.current.jump(() => virtualizer.scrollToIndex(row, { align: 'start' }));
    },
    [virtualizer, columns]
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

  useEffect(() => {
    if (scrollEl) scrollEl.scrollTop = 0;
  }, [generation, scrollEl]);

  const virtualRows = virtualizer.getVirtualItems();
  const firstRow = virtualRows.length > 0 ? virtualRows[0].index : -1;
  const lastRow = virtualRows.length > 0 ? virtualRows[virtualRows.length - 1].index : -1;
  useEffect(() => {
    if (firstRow >= 0) ensureRange(firstRow * columns, (lastRow + 1) * columns - 1);
  }, [firstRow, lastRow, columns, total, version, ensureRange]);

  const viewportHeight = virtualizer.scrollRect?.height ?? 0;
  const scrollOffset = virtualizer.scrollOffset ?? 0;
  const fits = viewportHeight === 0 || virtualizer.getTotalSize() <= viewportHeight + 1;
  const atEnd = !fits && scrollOffset + viewportHeight >= virtualizer.getTotalSize() - 2;
  // First row whose bottom is below the viewport top (+1px): the row at, or just entering, the top edge.
  const topRow = virtualRows.find((vr) => vr.end > scrollOffset + 1);
  const topIndex = (topRow ? topRow.index : 0) * columns;
  const viewport = useMemo<ListViewport>(
    () => ({ scrollElement: scrollEl, topIndex, atEnd, overlayRail, total, fits, scrollToOffset }),
    [scrollEl, topIndex, atEnd, overlayRail, total, fits, scrollToOffset]
  );

  const placeholder = renderPlaceholder ?? DEFAULT_PLACEHOLDER;
  const hasRows = total > 0;

  return (
    <ListViewportContext.Provider value={viewport}>
      <div ref={wrapperRef} className="relative flex items-stretch gap-1.5">
        <div className="flex-1 min-w-0 border border-[#222222] rounded-[4px] bg-[#141414] overflow-hidden">
          <div
            ref={setScrollEl}
            tabIndex={0}
            role="region"
            aria-label={`${ariaLabel} (scrollable)`}
            className="virtual-scroll relative p-0"
            style={{ maxHeight: maxHeight ?? (fillHeight !== null ? fillHeight - BOX_BORDER_PX : FALLBACK_MAX_HEIGHT) }}
          >
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
                role="list"
                aria-label={ariaLabel}
                aria-busy={loading}
                className="relative w-full"
                style={{ height: virtualizer.getTotalSize() }}
              >
                {virtualRows.map((vr) => {
                  const start = vr.index * columns;
                  const cells: React.ReactNode[] = [];
                  for (let i = start; i < Math.min(total, start + columns); i += 1) {
                    const item = getItem(i);
                    cells.push(
                      <div
                        key={item !== undefined ? getKey(item) : `ph-${i}`}
                        role="listitem"
                        className="min-w-0 overflow-hidden"
                        style={{ height: rowHeight }}
                      >
                        {item !== undefined ? renderItem(item, i) : placeholder(i)}
                      </div>
                    );
                  }
                  return (
                    <div
                      key={vr.index}
                      data-index={vr.index}
                      className="absolute top-0 grid"
                      style={{
                        height: rowHeight,
                        left: PAD,
                        width: `calc(100% - ${PAD * 2}px)`,
                        transform: `translateY(${vr.start}px)`,
                        gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
                        columnGap: gap,
                      }}
                    >
                      {cells}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
          {error !== null && hasRows && <ListErrorBar error={error} onRetry={retry} />}
        </div>
        {rail && (
          <aside className={overlayRail ? 'absolute right-px inset-y-px z-20' : 'shrink-0 self-stretch'}>{rail}</aside>
        )}
      </div>
    </ListViewportContext.Provider>
  );
}
