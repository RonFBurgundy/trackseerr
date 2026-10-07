import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import type { UseSystemActivityReturn } from '@/hooks/useSystemActivity';
import { BrandActivityPopover } from './BrandActivityPopover';

export interface BrandActivityProps {
  activity: Pick<UseSystemActivityReturn, 'running' | 'recent' | 'refresh'>;
  onOpenTasks: () => void;
  /** Classes for the trigger button (size / border differ between the desktop and mobile logo). */
  className?: string;
  children: React.ReactNode;
}

const POPOVER_WIDTH = 320;
const EDGE_GAP = 8;

/**
 * Admin-only logo trigger. While tasks run, amber sound-wave rings ripple outward from the logo (a static dot under
 * reduced motion). Clicking opens a popover of running tasks and recent runs.
 */
export const BrandActivity: React.FC<BrandActivityProps> = ({ activity, onOpenTasks, className = '', children }) => {
  const popoverId = useId();
  const [open, setOpen] = useState<boolean>(false);
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const isSheet = !useMediaQuery('(min-width: 768px)');
  const busy = activity.running.length > 0;
  const { refresh } = activity;

  const close = useCallback((restoreFocus: boolean): void => {
    setOpen(false);
    if (restoreFocus) buttonRef.current?.focus();
  }, []);

  const toggle = (): void => {
    if (open) {
      close(false);
      return;
    }
    setAnchor(buttonRef.current?.getBoundingClientRect() ?? null);
    setOpen(true);
    void refresh();
  };

  useEffect(() => {
    if (!open) return undefined;
    popoverRef.current?.focus();
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') close(true);
    };
    const onPointerDown = (e: PointerEvent): void => {
      const target = e.target;
      if (!(target instanceof Node)) return;
      if (popoverRef.current?.contains(target) || buttonRef.current?.contains(target)) return;
      close(false);
    };
    const onResize = (): void => close(false);
    document.addEventListener('keydown', onKey);
    document.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('resize', onResize);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('pointerdown', onPointerDown);
      window.removeEventListener('resize', onResize);
    };
  }, [open, close]);

  const top = (anchor?.bottom ?? 0) + EDGE_GAP;
  const style: React.CSSProperties = isSheet
    ? { top, left: 0, right: 0 }
    : { top, left: Math.max(EDGE_GAP, Math.min(anchor?.left ?? EDGE_GAP, window.innerWidth - POPOVER_WIDTH - EDGE_GAP)) };

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={toggle}
        aria-expanded={open}
        aria-controls={open ? popoverId : undefined}
        aria-haspopup="dialog"
        aria-label={busy ? `TrackSeerr activity: ${activity.running.length} task${activity.running.length === 1 ? '' : 's'} running` : 'TrackSeerr activity'}
        title="Background task activity"
        className={`relative ${className}`}
      >
        {busy && (
          <span aria-hidden="true" className="pointer-events-none absolute inset-0">
            <span className="brand-wave-ring" />
            <span className="brand-wave-ring" />
            <span className="brand-wave-ring" />
            <span className="brand-wave-dot" />
          </span>
        )}
        {children}
      </button>
      {open &&
        createPortal(
          <BrandActivityPopover
            ref={popoverRef}
            id={popoverId}
            running={activity.running}
            recent={activity.recent}
            style={style}
            isSheet={isSheet}
            onOpenTasks={() => {
              close(false);
              onOpenTasks();
            }}
          />,
          document.body
        )}
    </>
  );
};
