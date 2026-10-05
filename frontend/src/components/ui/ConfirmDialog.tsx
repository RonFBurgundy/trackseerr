import React, { useEffect, useId, useRef } from 'react';
import { Loader2 } from 'lucide-react';
import { TapeDeckButton } from './TapeDeckButton';
import { ActionBar } from './ActionBar';

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export interface ConfirmDialogProps {
  isOpen: boolean;
  title: string;
  children: React.ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  /** Optional middle action (e.g. "Future releases only"). */
  secondaryLabel?: string;
  onSecondary?: () => void;
  cancelLabel?: string;
  /** Called by the Cancel button and by Escape. */
  onCancel: () => void;
  busy?: boolean;
}

/** Small confirm modal: full-width on mobile, 4px radius on desktop, focus trapped, Escape cancels. */
export const ConfirmDialog: React.FC<ConfirmDialogProps> = ({
  isOpen,
  title,
  children,
  confirmLabel,
  onConfirm,
  secondaryLabel,
  onSecondary,
  cancelLabel = 'Cancel',
  onCancel,
  busy = false,
}) => {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const onCancelRef = useRef(onCancel);
  onCancelRef.current = onCancel;

  useEffect(() => {
    if (!isOpen) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const panel = panelRef.current;
    panel?.querySelector<HTMLElement>(FOCUSABLE)?.focus();

    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        e.preventDefault();
        onCancelRef.current();
        return;
      }
      if (e.key !== 'Tab' || !panelRef.current) return;
      const items = Array.from(panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || !panelRef.current.contains(active))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || !panelRef.current.contains(active))) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      previous?.focus();
    };
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-[60] flex items-end sm:items-center justify-center bg-black/85 backdrop-blur-[2px] p-0 sm:p-4">
      <div
        ref={panelRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="flex flex-col w-full max-w-none sm:max-w-md bg-[#121212] border-0 border-t sm:border border-[#2a2a2a] rounded-none sm:rounded-[4px] shadow-2xl pb-safe"
      >
        <div className="border-b border-[#222222] px-3 py-2.5 sm:px-5 sm:py-3 bg-[#181818]/80">
          <h3 id={titleId} className="text-sm sm:text-base font-bold tracking-wider uppercase text-white">
            {title}
          </h3>
        </div>
        <div className="p-4 sm:p-5 text-sm text-neutral-200">{children}</div>
        <ActionBar align="end" className="border-t border-[#222222] px-3 py-2.5 sm:px-5 sm:py-3 bg-[#0e0e0e] flex-wrap">
          <TapeDeckButton type="button" disabled={busy} onClick={onCancel}>
            {cancelLabel}
          </TapeDeckButton>
          {secondaryLabel && onSecondary && (
            <TapeDeckButton type="button" disabled={busy} onClick={onSecondary}>
              {secondaryLabel}
            </TapeDeckButton>
          )}
          <TapeDeckButton
            type="button"
            variant="amber"
            disabled={busy}
            onClick={onConfirm}
            icon={busy ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
          >
            {confirmLabel}
          </TapeDeckButton>
        </ActionBar>
      </div>
    </div>
  );
};
