import React, { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { useModalHistory } from '@/hooks/useModalHistory';
import { TapeDeckButton } from './TapeDeckButton';
import { ActionBar } from './ActionBar';

// Open modals, topmost last. Only the topmost handles Escape.
const openModalStack: symbol[] = [];

export interface ObsidianModalProps {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  maxWidth?: string;
  /**
   * Default true: Back (browser, OS gesture, mouse button) closes the modal via a sentinel history entry. Pass false
   * when the modal's open state is already driven by the URL.
   */
  historyBacked?: boolean;
}

export const ObsidianModal: React.FC<ObsidianModalProps> = ({
  isOpen,
  onClose,
  title,
  subtitle,
  children,
  footer,
  maxWidth = 'sm:max-w-2xl',
  historyBacked = true,
}) => {
  const idRef = useRef<symbol>(Symbol('obsidian-modal'));

  useEffect(() => {
    if (!isOpen) return;
    const id = idRef.current;
    openModalStack.push(id);
    return () => {
      const idx = openModalStack.indexOf(id);
      if (idx !== -1) openModalStack.splice(idx, 1);
    };
  }, [isOpen]);

  useModalHistory(isOpen, onClose, historyBacked);

  // Return focus to whatever opened the modal once it closes.
  useEffect(() => {
    if (!isOpen) return undefined;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    return () => {
      if (opener && opener.isConnected) opener.focus();
    };
  }, [isOpen]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (
        e.key === 'Escape' &&
        isOpen &&
        openModalStack[openModalStack.length - 1] === idRef.current
      ) {
        onClose();
      }
    };
    if (isOpen) {
      window.addEventListener('keydown', handleKeyDown);
    }
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 backdrop-blur-[2px] p-0 sm:p-4 overflow-hidden overscroll-contain"
      role="dialog"
      aria-modal="true"
    >
      <div
        className={`relative flex flex-col w-full h-full sm:h-auto sm:max-h-[90dvh] bg-[#121212] border-0 sm:border sm:border-[#2a2a2a] rounded-none sm:rounded-[4px] shadow-2xl pt-safe pb-safe ${maxWidth}`}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-[#222222] px-3 py-2.5 sm:px-5 sm:py-3 sm:py-3.5 bg-[#181818]/80 shrink-0">
          <div className="min-w-0">
            <h3 className="text-sm sm:text-base font-bold tracking-wider uppercase text-white">
              {title}
            </h3>
            {subtitle && (
              <p className="text-xs text-neutral-400 mt-0.5">{subtitle}</p>
            )}
          </div>
          <TapeDeckButton
            size="sm"
            onClick={onClose}
            className="shrink-0"
            aria-label="Close dialog"
            icon={<X className="h-4 w-4" />}
          />
        </div>

        {/* Scrollable Body */}
        <div className="modal-body-scroll flex-1 min-h-0 p-4 sm:p-5 text-neutral-200">
          {children}
        </div>

        {/* Footer */}
        {footer && (
          <ActionBar
            align="end"
            className="border-t border-[#222222] px-3 py-2.5 pb-safe sm:px-5 sm:py-3 bg-[#0e0e0e] shrink-0"
          >
            {footer}
          </ActionBar>
        )}
      </div>
    </div>,
    document.body
  );
};
