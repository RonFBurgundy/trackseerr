import { useCallback, useEffect, useState } from 'react';

/** What to find on the destination page. Mirrors `SettingTarget` (kept here so hooks never import components). */
export interface HighlightTarget {
  elementId?: string;
  anchor?: string;
  /** Changes on every request so selecting the same setting twice pulses twice. */
  nonce: number;
}

const GIVE_UP_MS = 5000;
const PULSE_MS = 2200;
const SEARCH_SELECTOR = 'label, h2, h3, h4, legend, summary, button, span, p';
const CLASS = 'setting-pulse';

const norm = (s: string): string => s.replace(/\s+/g, ' ').trim().toLowerCase();

/** Locate the element to pulse: a static id first, then the first label/heading whose text starts with the anchor. */
export function findSettingElement(target: HighlightTarget): HTMLElement | null {
  if (target.elementId) {
    const byId = document.getElementById(target.elementId);
    if (byId) return byId.closest<HTMLElement>('div') ?? byId;
  }
  const anchor = target.anchor ? norm(target.anchor) : '';
  const body = document.querySelector<HTMLElement>('[data-page-body]');
  if (!anchor || !body) return null;
  const matches = Array.from(body.querySelectorAll<HTMLElement>(SEARCH_SELECTOR)).filter((el) => {
    const text = norm(el.textContent ?? '');
    return text.startsWith(anchor) && text.length <= anchor.length + 40;
  });
  // The deepest match is the label itself, not a wrapper that happens to start with the same text.
  const el = matches.find((m) => !m.querySelector(SEARCH_SELECTOR) || m.matches('label, h2, h3, h4, legend')) ?? matches[0];
  if (!el) return null;
  if (el.matches('h2, h3, h4, legend, summary, button')) return el;
  return el.parentElement && el.parentElement !== body ? el.parentElement : el;
}

/**
 * Pulse the requested setting once the page has rendered it. Settings load asynchronously, so the lookup retries on
 * DOM changes until it finds the element or gives up.
 */
export function useSettingHighlight(): { highlight: (target: Omit<HighlightTarget, 'nonce'>) => void } {
  const [target, setTarget] = useState<HighlightTarget | null>(null);

  const highlight = useCallback((t: Omit<HighlightTarget, 'nonce'>): void => {
    setTarget({ ...t, nonce: Date.now() });
  }, []);

  useEffect(() => {
    if (!target) return undefined;
    let done = false;
    let pulseTimer: number | null = null;
    let current: HTMLElement | null = null;

    const attempt = (): void => {
      if (done) return;
      const el = findSettingElement(target);
      if (!el) return;
      done = true;
      observer.disconnect();
      current = el;
      el.scrollIntoView({ block: 'center', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
      el.classList.remove(CLASS);
      // Force a reflow so re-adding the class restarts the animation.
      void el.offsetWidth;
      el.classList.add(CLASS);
      pulseTimer = window.setTimeout(() => el.classList.remove(CLASS), PULSE_MS);
    };

    const observer = new MutationObserver(attempt);
    observer.observe(document.body, { childList: true, subtree: true });
    const first = window.setTimeout(attempt, 60);
    const giveUp = window.setTimeout(() => {
      done = true;
      observer.disconnect();
    }, GIVE_UP_MS);
    return () => {
      done = true;
      observer.disconnect();
      window.clearTimeout(first);
      window.clearTimeout(giveUp);
      if (pulseTimer !== null) window.clearTimeout(pulseTimer);
      current?.classList.remove(CLASS);
    };
  }, [target]);

  return { highlight };
}
