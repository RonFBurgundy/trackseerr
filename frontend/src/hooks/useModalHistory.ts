import { useEffect, useRef } from 'react';

/** History-state key marking a sentinel entry pushed for an open modal; the value is the modal's nesting depth. */
const DEPTH_KEY = '__tsModalDepth';

interface ModalEntry {
  depth: number;
  /** Close request from a history pop (browser/OS back, mouse back button, Alt+Left). */
  onPop: () => void;
  /** The sentinel was already consumed by a pop, so closing must not call `history.back()` again. */
  popped: boolean;
  /** The sentinel has been pushed (activation can be deferred while one of our own `back()` calls is in flight). */
  active: boolean;
  /** The modal closed before the deferred activation ran. */
  cancelled: boolean;
}

/** Active entries, outermost first. */
const entries: ModalEntry[] = [];
/** Number of `history.back()` calls we issued whose popstate has not arrived yet. */
let pendingBacks = 0;
/** Sentinel pushes waiting for in-flight `back()` calls to settle, so a push is never undone by a stale back. */
const deferred: Array<() => void> = [];
let listening = false;

function depthOf(state: unknown): number {
  if (typeof state === 'object' && state !== null && DEPTH_KEY in state) {
    const value = state[DEPTH_KEY];
    return typeof value === 'number' ? value : 0;
  }
  return 0;
}

function handlePopState(): void {
  if (pendingBacks > 0) pendingBacks -= 1;
  const current = depthOf(window.history.state);
  // Close every modal whose sentinel is now ahead of us, topmost first. Normally that is exactly one.
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    const entry = entries[i];
    if (entry.depth > current && !entry.popped) {
      entry.popped = true;
      entry.onPop();
    }
  }
  if (pendingBacks === 0) {
    // Forward navigation onto a sentinel whose modal is gone: step back off it rather than strand the user.
    const stranded = current > 0 && !entries.some((entry) => entry.depth === current);
    if (stranded) {
      pendingBacks += 1;
      window.history.back();
      return;
    }
    const waiting = deferred.splice(0, deferred.length);
    waiting.forEach((run) => run());
  }
}

function ensureListener(): void {
  if (listening) return;
  listening = true;
  window.addEventListener('popstate', handlePopState);
}

/**
 * Make the browser/OS Back action close a modal instead of leaving the page.
 *
 * While `isOpen`, a sentinel history entry (same URL, state `{ __tsModalDepth: n }`) sits on top of the stack:
 * - popstate that removes the sentinel closes the modal (`onClose`); only the topmost of nested modals reacts, because
 *   each modal owns its own depth and one Back removes exactly one entry;
 * - closing any other way (X, Escape, backdrop, Cancel) consumes the sentinel with `history.back()`, but only when it
 *   is still the current entry and was not already popped, so it never double-pops or eats a real navigation;
 * - a modal opening while one of our own `back()` calls is still in flight waits for it, so push and back cannot cross.
 *
 * Pass `enabled = false` for modals whose open state is already driven by the URL.
 */
export function useModalHistory(isOpen: boolean, onClose: () => void, enabled: boolean = true): void {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!isOpen || !enabled) return undefined;
    ensureListener();
    const entry: ModalEntry = {
      depth: 0,
      onPop: () => closeRef.current(),
      popped: false,
      active: false,
      cancelled: false,
    };
    const activate = (): void => {
      if (entry.cancelled) return;
      const top = entries[entries.length - 1];
      entry.depth = (top ? top.depth : depthOf(window.history.state)) + 1;
      window.history.pushState({ [DEPTH_KEY]: entry.depth }, '');
      entry.active = true;
      entries.push(entry);
    };
    if (pendingBacks > 0) deferred.push(activate);
    else activate();

    return () => {
      entry.cancelled = true;
      if (!entry.active) return;
      const index = entries.indexOf(entry);
      if (index !== -1) entries.splice(index, 1);
      if (!entry.popped && depthOf(window.history.state) === entry.depth) {
        pendingBacks += 1;
        window.history.back();
      }
    };
  }, [isOpen, enabled]);
}
