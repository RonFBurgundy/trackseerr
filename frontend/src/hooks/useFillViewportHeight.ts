import { useCallback, useLayoutEffect, useState, type RefObject } from 'react';

/** Floor (px) for a filled list on a tiny viewport. */
const MIN_FILL_PX = 220;

function isScrollable(el: HTMLElement): boolean {
  const overflowY = getComputedStyle(el).overflowY;
  return overflowY === 'auto' || overflowY === 'scroll';
}

/** The nearest scrolling ancestor (a PageFrame body counts even when it clips), or null when none is found. */
function findScrollParent(el: HTMLElement): HTMLElement | null {
  let node: HTMLElement | null = el.parentElement;
  while (node && node !== document.body && node !== document.documentElement) {
    if (isScrollable(node) || node.hasAttribute('data-page-body')) return node;
    node = node.parentElement;
  }
  return null;
}

/**
 * Height (px) that makes `ref`'s box end exactly at the bottom of the visible area, so a list placed there fills the
 * remaining viewport and the page itself does not scroll. The bottom edge is the scrolling ancestor's content box
 * (its bottom padding, which carries the safe-area inset and the audio bar's room, is respected); with no scrolling
 * ancestor it is the visual viewport. Recomputed on resize, orientation change, visual-viewport changes and when
 * anything above the box changes height. Null until the first measurement.
 */
export function useFillViewportHeight(ref: RefObject<HTMLElement | null>, minPx: number = MIN_FILL_PX): number | null {
  const [height, setHeight] = useState<number | null>(null);

  const measure = useCallback((): void => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const parent = findScrollParent(el);
    let available: number;
    if (parent) {
      const parentRect = parent.getBoundingClientRect();
      const padBottom = parseFloat(getComputedStyle(parent).paddingBottom) || 0;
      const bottom = parentRect.top + parent.clientHeight - padBottom;
      available = bottom - rect.top;
    } else {
      const viewportH = window.visualViewport?.height ?? window.innerHeight;
      available = viewportH - rect.top;
    }
    const next = Math.max(minPx, Math.floor(available));
    setHeight((prev) => (prev === next ? prev : next));
  }, [ref, minPx]);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    measure();
    const parent = findScrollParent(el);
    const onChange = (): void => measure();
    window.addEventListener('resize', onChange);
    window.addEventListener('orientationchange', onChange);
    window.visualViewport?.addEventListener('resize', onChange);
    parent?.addEventListener('scroll', onChange, { passive: true });
    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(onChange);
      if (parent) observer.observe(parent);
      if (el.parentElement) observer.observe(el.parentElement);
      for (let sib = el.previousElementSibling; sib; sib = sib.previousElementSibling) observer.observe(sib);
    }
    return () => {
      window.removeEventListener('resize', onChange);
      window.removeEventListener('orientationchange', onChange);
      window.visualViewport?.removeEventListener('resize', onChange);
      parent?.removeEventListener('scroll', onChange);
      observer?.disconnect();
    };
  }, [ref, measure]);

  // Padding of the scrolling ancestor can change without a resize (the audio bar appears); re-read each commit.
  useLayoutEffect(() => {
    measure();
  });

  return height;
}
