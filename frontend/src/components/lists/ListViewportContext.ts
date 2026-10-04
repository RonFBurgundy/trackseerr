import { createContext, useContext } from 'react';

/**
 * Viewport state a virtualized list (FlatList, VirtualGrid) publishes to its `rail` slot, so a ScrubberRail does
 * not need the list's internals.
 */
export interface ListViewport {
  /** The element that scrolls the rows; null until mounted. */
  scrollElement: HTMLElement | null;
  /** Index of the first item whose row contains the viewport top (+1px); a row cut off above is skipped. */
  topIndex: number;
  /** True when scrolled to the very end of the content. */
  atEnd: boolean;
  /** True on narrow screens, where the rail is a slim unboxed overlay on the list's right edge. */
  overlayRail: boolean;
  total: number;
  /** True when all content fits without scrolling. */
  fits: boolean;
  /** Jumps so the item at `index` is at the top. Coordinates with the virtualizer. */
  scrollToOffset: (index: number) => void;
}

export const ListViewportContext = createContext<ListViewport | null>(null);

export function useListViewport(): ListViewport | null {
  return useContext(ListViewportContext);
}
