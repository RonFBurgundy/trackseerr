import React from 'react';
import { createPortal } from 'react-dom';

export interface LibraryToolbarPortalProps {
  /** The `display: contents` slot owned by the page toolbar; null until it mounts. */
  slot: HTMLElement | null;
  children: React.ReactNode;
}

/** Renders a panel's controls (select mode, sort) inside the page's single toolbar row. */
export const LibraryToolbarPortal: React.FC<LibraryToolbarPortalProps> = ({ slot, children }) =>
  slot ? createPortal(children, slot) : null;

export interface LibrarySelectAction {
  active: boolean;
  toggle: () => void;
}
