import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';

/** Pinned chrome (app header + page nav + actions) may use at most this share of the viewport height. */
export const CHROME_BUDGET_RATIO = 0.25;
/** Cumulative downward scroll (px) before the actions row hides. */
const HIDE_AFTER_DOWN_PX = 24;
/** Upward scroll (px) that reveals it again. */
const REVEAL_AFTER_UP_PX = 8;
/** Scroll events inside this window after a toggle are layout fallout (the body grows or shrinks), not user intent. */
const SETTLE_MS = 200;

export interface CollapsibleChromeRefs {
  /** Frame root: its top edge already includes the app header and main padding. */
  root: RefObject<HTMLElement | null>;
  /** Wrapper of the always-pinned nav row (may be absent). */
  nav: RefObject<HTMLElement | null>;
  /** Natural-height content of the actions row (measured even while the row is collapsed). */
  actionsInner: RefObject<HTMLElement | null>;
  /** The collapsible wrapper (scroll events inside it are ignored; focus inside it blocks collapsing). */
  actionsWrap: RefObject<HTMLElement | null>;
}

export interface CollapsibleChromeState {
  /** True when pinned chrome would take more than the budget of the viewport. */
  overBudget: boolean;
  /** True while the actions row is hidden (only ever true when over budget). */
  collapsed: boolean;
  /** Reveal the row (focus moved into it, or a caller wants it back). */
  reveal: () => void;
}

/**
 * Chrome budget: measures header + nav + actions against the visual viewport and, only when over budget, hides the
 * actions row on scroll down and reveals it on scroll up. Under budget it just measures (resize / observer events).
 */
export function useCollapsibleChrome(
  refs: CollapsibleChromeRefs
): CollapsibleChromeState {
  const { root, nav, actionsInner, actionsWrap } = refs;
  const [overBudget, setOverBudget] = useState<boolean>(false);
  const [collapsed, setCollapsed] = useState<boolean>(false);
  const collapsedRef = useRef<boolean>(false);
  const settleUntil = useRef<number>(0);

  const apply = useCallback((next: boolean): void => {
    if (collapsedRef.current === next) return;
    collapsedRef.current = next;
    settleUntil.current = performance.now() + SETTLE_MS;
    setCollapsed(next);
  }, []);

  const measure = useCallback((): void => {
    const rootEl = root.current;
    const actionsH = actionsInner.current?.offsetHeight ?? 0;
    // No actions (nothing portaled in, nothing passed): nothing to collapse.
    if (rootEl) {
      // Lets an outer frame's scroll handler defer to the innermost frame that owns an actions row.
      if (actionsH > 0) rootEl.setAttribute('data-has-actions', '');
      else rootEl.removeAttribute('data-has-actions');
    }
    if (!rootEl || actionsH === 0) {
      setOverBudget(false);
      return;
    }
    const viewportH = window.visualViewport?.height ?? window.innerHeight;
    const chrome = rootEl.getBoundingClientRect().top + (nav.current?.offsetHeight ?? 0) + actionsH;
    setOverBudget(chrome > viewportH * CHROME_BUDGET_RATIO);
  }, [root, nav, actionsInner]);

  useLayoutEffect(() => {
    measure();
    const onChange = (): void => measure();
    window.addEventListener('resize', onChange);
    window.addEventListener('orientationchange', onChange);
    window.visualViewport?.addEventListener('resize', onChange);
    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(onChange);
      for (const ref of [root, nav, actionsInner]) if (ref.current) observer.observe(ref.current);
    }
    return () => {
      window.removeEventListener('resize', onChange);
      window.removeEventListener('orientationchange', onChange);
      window.visualViewport?.removeEventListener('resize', onChange);
      observer?.disconnect();
    };
  }, [measure, root, nav, actionsInner]);

  // Back under budget (or actions gone): always visible.
  useEffect(() => {
    if (!overBudget) apply(false);
  }, [overBudget, apply]);

  // Scroll does not bubble but is captured by ancestors, so one listener on the frame root sees every descendant
  // scroller (the body, a virtual grid, a log list). Each target's last scrollTop is tracked so horizontal-only
  // scrollers (tab strips) produce a zero vertical delta and are ignored.
  useEffect(() => {
    if (!overBudget) return undefined;
    const rootEl = root.current;
    if (!rootEl) return undefined;
    const lastTop = new WeakMap<Element, number>();
    let down = 0;
    let up = 0;
    const onScroll = (event: Event): void => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      // The innermost frame that has an actions row owns the event; outer frames ignore it.
      if (target.closest('[data-page-frame][data-has-actions]') !== rootEl) return;
      // Scrolling inside the actions row itself (focus scrolling an overflow-hidden row) is not user intent.
      if (actionsWrap.current?.contains(target)) return;
      const top = target.scrollTop;
      const prev = lastTop.get(target) ?? top;
      lastTop.set(target, top);
      if (performance.now() < settleUntil.current) return;
      const delta = top - prev;
      if (delta === 0) return;
      const actionsH = actionsInner.current?.offsetHeight ?? 0;
      if (top < actionsH) {
        down = 0;
        up = 0;
        apply(false);
        return;
      }
      if (delta > 0) {
        down += delta;
        up = 0;
        // Only hide when the target will still scroll after it grows by the row's height (no flicker at the end),
        // and never while focus sits inside the row.
        const spare = target.scrollHeight - target.clientHeight;
        const focusInRow = actionsWrap.current?.contains(document.activeElement) ?? false;
        if (down > HIDE_AFTER_DOWN_PX && spare > actionsH + 48 && !focusInRow) apply(true);
      } else {
        up -= delta;
        down = 0;
        if (up >= REVEAL_AFTER_UP_PX) apply(false);
      }
    };
    rootEl.addEventListener('scroll', onScroll, { passive: true, capture: true });
    return () => rootEl.removeEventListener('scroll', onScroll, { capture: true });
  }, [overBudget, root, actionsWrap, actionsInner, apply]);

  const reveal = useCallback((): void => apply(false), [apply]);

  return { overBudget, collapsed, reveal };
}
