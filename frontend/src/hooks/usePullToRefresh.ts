import { useEffect, useRef, useState, type RefObject } from 'react';

/** Resisted pull distance (px) that arms a refresh. */
export const PULL_THRESHOLD_PX = 70;
const PULL_MAX_PX = 110;
const HOLD_PX = 48;
const MIN_REFRESH_MS = 600;
const LOCK_PX = 8;

export type PullPhase = 'idle' | 'pulling' | 'armed' | 'refreshing';

export interface PullState {
  /** Resisted distance the indicator should be offset by. */
  distance: number;
  phase: PullPhase;
}

/** Touches that start here never pull: inputs, scrubber rails, sliders, anything that owns its own touch gestures. */
const IGNORE = 'input, textarea, select, [contenteditable="true"], [role="slider"], [data-no-ptr], .scrubber-rail, .touch-none';

/** Diminishing-returns curve: nearly 1:1 at first, asymptote at PULL_MAX_PX. */
export function resist(rawDy: number): number {
  return PULL_MAX_PX * (1 - Math.exp(-rawDy / (PULL_MAX_PX * 1.4)));
}

/** True when every vertically scrollable ancestor of `el` (up to `root`) is at its top. */
function atTop(el: Element, root: Element): boolean {
  for (let node: Element | null = el; node && node !== root.parentElement; node = node.parentElement) {
    if (node instanceof HTMLElement && node.scrollHeight > node.clientHeight) {
      const oy = getComputedStyle(node).overflowY;
      if ((oy === 'auto' || oy === 'scroll') && node.scrollTop > 0) return false;
    }
  }
  return true;
}

/** True when a modal, drawer or confirm dialog is open (their backdrops sit outside `root`). */
const overlayOpen = (): boolean => document.querySelector('[aria-modal="true"]') !== null;

export interface UsePullToRefreshOptions {
  /** Element whose page bodies are pulled (the app `main`). */
  rootRef: RefObject<HTMLElement | null>;
  /** Runs on release past the threshold; the indicator stays until it settles. */
  onRefresh: () => Promise<void>;
  enabled?: boolean;
}

/**
 * Touch pull-down at the top of the page body. Vertical-only (a horizontal swipe cancels it), skips inputs and
 * scrubber rails, and never starts while any modal is open or an inner scroller is mid-scroll.
 */
export function usePullToRefresh({ rootRef, onRefresh, enabled = true }: UsePullToRefreshOptions): PullState {
  const [state, setState] = useState<PullState>({ distance: 0, phase: 'idle' });
  const refreshRef = useRef(onRefresh);
  refreshRef.current = onRefresh;
  const busy = useRef(false);

  useEffect(() => {
    const root = rootRef.current;
    if (!root || !enabled) return undefined;
    let startX = 0;
    let startY = 0;
    let tracking = false;
    let locked = false;
    let distance = 0;

    const reset = (): void => {
      tracking = false;
      locked = false;
      distance = 0;
      setState({ distance: 0, phase: 'idle' });
    };

    const onStart = (e: TouchEvent): void => {
      tracking = false;
      if (busy.current || e.touches.length !== 1 || !(e.target instanceof Element)) return;
      if (e.target.closest(IGNORE) || overlayOpen() || !atTop(e.target, root)) return;
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
      tracking = true;
      locked = false;
      distance = 0;
    };

    const onMove = (e: TouchEvent): void => {
      if (!tracking || e.touches.length !== 1) return;
      const dx = e.touches[0].clientX - startX;
      const dy = e.touches[0].clientY - startY;
      if (!locked) {
        if (Math.abs(dx) < LOCK_PX && Math.abs(dy) < LOCK_PX) return;
        // Upward or mostly horizontal: this gesture belongs to scrolling/swiping, not to us.
        if (dy <= 0 || Math.abs(dx) > Math.abs(dy)) {
          tracking = false;
          return;
        }
        locked = true;
      }
      if (dy <= 0) {
        reset();
        tracking = false;
        return;
      }
      if (e.cancelable) e.preventDefault();
      distance = resist(dy);
      setState({ distance, phase: distance >= PULL_THRESHOLD_PX ? 'armed' : 'pulling' });
    };

    const onEnd = (): void => {
      if (!tracking) return;
      const armed = locked && distance >= PULL_THRESHOLD_PX;
      tracking = false;
      if (!armed) {
        reset();
        return;
      }
      busy.current = true;
      setState({ distance: HOLD_PX, phase: 'refreshing' });
      const started = Date.now();
      void refreshRef
        .current()
        .catch((err: unknown) => {
          console.error('Pull-to-refresh failed', err);
        })
        .finally(() => {
          const wait = Math.max(0, MIN_REFRESH_MS - (Date.now() - started));
          window.setTimeout(() => {
            busy.current = false;
            reset();
          }, wait);
        });
    };

    root.addEventListener('touchstart', onStart, { passive: true });
    root.addEventListener('touchmove', onMove, { passive: false });
    root.addEventListener('touchend', onEnd, { passive: true });
    root.addEventListener('touchcancel', onEnd, { passive: true });
    return () => {
      root.removeEventListener('touchstart', onStart);
      root.removeEventListener('touchmove', onMove);
      root.removeEventListener('touchend', onEnd);
      root.removeEventListener('touchcancel', onEnd);
    };
  }, [rootRef, enabled]);

  return state;
}
