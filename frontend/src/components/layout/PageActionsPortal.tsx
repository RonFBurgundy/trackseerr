import React, { createContext, useContext } from 'react';
import { createPortal } from 'react-dom';

/** The element inside the nearest PageFrame's actions row that panels portal their controls into. */
export const PageActionsSlotContext = createContext<HTMLElement | null>(null);

export interface PageActionsPortalProps {
  children: React.ReactNode;
}

/**
 * Renders a panel's action row (add, refresh, save) into the enclosing PageFrame's pinned actions row, so it stays put
 * while the panel scrolls. Renders nothing until the frame has mounted its slot, or outside a frame.
 */
export const PageActionsPortal: React.FC<PageActionsPortalProps> = ({ children }) => {
  const slot = useContext(PageActionsSlotContext);
  return slot ? createPortal(children, slot) : null;
};
