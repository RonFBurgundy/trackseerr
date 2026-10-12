import React, { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { MoreVertical } from 'lucide-react';

export interface OverflowMenuItem {
  key: string;
  label: string;
  icon?: React.ReactNode;
  disabled?: boolean;
  /** Reflects a toggled state (role menuitemcheckbox). */
  checked?: boolean;
  onSelect: () => void;
}

const GUTTER = 16;
const GAP = 4;

interface MenuPosition {
  top: number;
  right: number;
}

export interface OverflowMenuProps {
  items: OverflowMenuItem[];
  /** Accessible name of the trigger button. */
  label: string;
  className?: string;
}

/** Three-dot trigger with a small popup menu. Escape and outside click close it. */
export const OverflowMenu: React.FC<OverflowMenuProps> = ({ items, label, className = '' }) => {
  const [open, setOpen] = useState<boolean>(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();
  const [pos, setPos] = useState<MenuPosition | null>(null);

  const close = useCallback((): void => setOpen(false), []);

  const place = useCallback((): void => {
    const trigger = triggerRef.current;
    const menu = menuRef.current;
    if (!trigger || !menu) return;
    const rect = trigger.getBoundingClientRect();
    const height = menu.offsetHeight;
    const width = menu.offsetWidth;
    const maxRight = Math.max(GUTTER, window.innerWidth - width - GUTTER);
    const right = Math.min(Math.max(GUTTER, window.innerWidth - rect.right), maxRight);
    const below = rect.bottom + GAP;
    const fitsBelow = below + height <= window.innerHeight - GUTTER;
    const above = rect.top - GAP - height;
    let top = fitsBelow || above < GUTTER ? below : above;
    top = Math.max(GUTTER, Math.min(top, window.innerHeight - GUTTER - height));
    setPos({ top, right });
  }, []);

  // Measure after the menu mounts, before paint, so it never flashes at the wrong spot.
  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onScroll = (e: Event): void => {
      // Scrolling inside the menu itself must not dismiss it.
      if (e.target instanceof Node && menuRef.current?.contains(e.target)) return;
      close();
    };
    window.addEventListener('resize', place);
    window.addEventListener('scroll', onScroll, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', onScroll, true);
    };
  }, [open, place, close]);

  // Focus moves to the first enabled item once the menu is positioned (hidden elements cannot focus).
  useEffect(() => {
    if (!open || !pos) return;
    const first = menuRef.current?.querySelector<HTMLElement>('[role^="menuitem"]:not(:disabled)');
    if (first && !menuRef.current?.contains(document.activeElement)) first.focus();
  }, [open, pos]);

  const onMenuKeyDown = (e: React.KeyboardEvent<HTMLElement>): void => {
    if (e.key === 'Escape') {
      // Only the menu closes: an enclosing modal must not see this key.
      e.preventDefault();
      e.stopPropagation();
      close();
      triggerRef.current?.focus();
      return;
    }
    if (e.key === 'Tab') {
      close();
      return;
    }
    const entries = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role^="menuitem"]:not(:disabled)') ?? []);
    if (entries.length === 0) return;
    const current = entries.findIndex((el) => el === document.activeElement);
    let next: number | null = null;
    if (e.key === 'ArrowDown') next = (current + 1) % entries.length;
    else if (e.key === 'ArrowUp') next = current <= 0 ? entries.length - 1 : current - 1;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = entries.length - 1;
    if (next === null) return;
    e.preventDefault();
    entries[next].focus();
  };

  useEffect(() => {
    if (!open) return;
    const isInside = (node: Node): boolean => !!rootRef.current?.contains(node) || !!menuRef.current?.contains(node);
    const onPointer = (e: MouseEvent | TouchEvent): void => {
      if (e.target instanceof Node && !isInside(e.target)) close();
    };
    const onFocusIn = (e: FocusEvent): void => {
      if (e.target instanceof Node && !isInside(e.target)) close();
    };
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('touchstart', onPointer);
    document.addEventListener('focusin', onFocusIn);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('touchstart', onPointer);
      document.removeEventListener('focusin', onFocusIn);
    };
  }, [open, close]);

  if (items.length === 0) return null;

  return (
    <div ref={rootRef} className={`relative ${className}`} onKeyDown={open ? onMenuKeyDown : undefined}>
      <button
        ref={triggerRef}
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex h-9 w-9 items-center justify-center rounded-[3px] text-neutral-400 hover:text-white hover:bg-[#1c1c1c] focus:outline-none focus-visible:ring-1 focus-visible:ring-[#e5a00d]"
      >
        <MoreVertical className="h-5 w-5" />
      </button>
      {open &&
        createPortal(
        <div
          id={menuId}
          ref={menuRef}
          role="menu"
          aria-label={label}
          style={{ top: pos?.top ?? 0, right: pos?.right ?? 0, visibility: pos ? 'visible' : 'hidden' }}
          className="fixed z-40 min-w-[11rem] rounded-[4px] border border-[#2a2a2a] bg-[#181818] py-1 shadow-[0_8px_20px_rgba(0,0,0,0.6)]"
        >
          {items.map((item) => (
            <button
              key={item.key}
              type="button"
              role={item.checked === undefined ? 'menuitem' : 'menuitemcheckbox'}
              aria-checked={item.checked}
              disabled={item.disabled}
              onClick={() => {
                close();
                item.onSelect();
              }}
              className="flex min-h-[36px] w-full items-center gap-2 px-3 text-left text-xs font-mono text-neutral-200 hover:bg-[#1c1c1c] disabled:opacity-50 focus:outline-none focus-visible:bg-[#1c1c1c]"
            >
              {item.icon}
              <span className="flex-1">{item.label}</span>
              {item.checked !== undefined && (
                <span className={item.checked ? 'text-[#e5a00d]' : 'text-neutral-500'}>{item.checked ? 'On' : 'Off'}</span>
              )}
            </button>
          ))}
        </div>,
        document.body
      )}
    </div>
  );
};
