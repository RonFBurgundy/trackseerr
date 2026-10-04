/**
 * Jumps to a row, then re-issues the same jump while measurements settle. A virtualizer positions a far, never
 * rendered row from estimated heights; once the rows around it mount and are measured the target's offset moves, so
 * the first jump can land a row early. Re-issuing after the layout commits lands it exactly. A newer jump cancels the
 * pending re-issues of an older one.
 */
const SETTLE_DELAYS_MS: readonly number[] = [0, 60, 160, 360];

export interface ScrollJumper {
  jump: (run: () => void) => void;
  cancel: () => void;
}

export function createScrollJumper(): ScrollJumper {
  let token = 0;
  const timers = new Set<number>();
  const clear = (): void => {
    for (const t of timers) window.clearTimeout(t);
    timers.clear();
  };
  return {
    jump: (run) => {
      token += 1;
      const mine = token;
      clear();
      run();
      for (const delay of SETTLE_DELAYS_MS) {
        const timer = window.setTimeout(() => {
          timers.delete(timer);
          if (token === mine) run();
        }, delay);
        timers.add(timer);
      }
    },
    cancel: () => {
      token += 1;
      clear();
    },
  };
}

/**
 * Cancels pending settle re-issues when the user takes over scrolling (wheel, touch, keyboard) on `el`, so a delayed
 * re-issue never snaps the list back. Deliberately not bound to pointerdown: the scrubber rail sits beside the scroll
 * element, and its own drag must keep issuing jumps. Returns the unbind function.
 */
export function cancelJumpsOnUserScroll(el: HTMLElement, jumper: ScrollJumper): () => void {
  const cancel = (): void => jumper.cancel();
  const events = ['wheel', 'touchstart', 'keydown'] as const;
  for (const name of events) el.addEventListener(name, cancel, { passive: true });
  return () => {
    for (const name of events) el.removeEventListener(name, cancel);
  };
}
