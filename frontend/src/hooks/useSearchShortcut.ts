import { useEffect } from 'react';

/** Marks the search box a page exposes to the `/` shortcut (set by `SearchBar`). */
export const PAGE_SEARCH_ATTR = 'data-page-search';

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
}

/** `/` focuses the current page's search input, unless the user is typing or a modal is open. */
export function useSearchShortcut(): void {
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key !== '/' || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
      if (isTypingTarget(e.target)) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      const inputs = document.querySelectorAll<HTMLInputElement>(`input[${PAGE_SEARCH_ATTR}]:not([disabled])`);
      const visible = Array.from(inputs).find((el) => el.getClientRects().length > 0);
      if (!visible) return;
      e.preventDefault();
      visible.focus();
      visible.select();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);
}
