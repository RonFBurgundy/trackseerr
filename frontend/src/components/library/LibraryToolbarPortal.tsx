import React from 'react';
import { createPortal } from 'react-dom';
import { CheckSquare } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';

export interface LibraryToolbarPortalProps {
  /** The `display: contents` slot owned by the page toolbar; null until it mounts. */
  slot: HTMLElement | null;
  children: React.ReactNode;
}

/** Renders a panel's controls (select mode, sort) inside the page's single toolbar row. */
export const LibraryToolbarPortal: React.FC<LibraryToolbarPortalProps> = ({ slot, children }) =>
  slot ? createPortal(children, slot) : null;

export interface LibrarySelectKeyProps {
  active: boolean;
  onToggle: () => void;
}

/** Bulk-select mode key; icon-only on phones. */
export const LibrarySelectKey: React.FC<LibrarySelectKeyProps> = ({ active, onToggle }) => (
  <TapeDeckButton
    size="sm"
    className="shrink-0"
    active={active}
    aria-pressed={active}
    aria-label="Select items"
    title="Select items"
    icon={<CheckSquare className="h-3.5 w-3.5" />}
    collapseLabel
    onClick={onToggle}
  >
    Select
  </TapeDeckButton>
);
