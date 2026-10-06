import React, { useCallback, useRef, useState, type MutableRefObject } from 'react';
import { useCollapsibleChrome } from '@/hooks/useCollapsibleChrome';
import { PageActionsSlotContext } from './PageActionsPortal';

export interface PageFrameProps {
  /** Always-pinned chrome above the body (tab strips). Never scrolls, never collapses. */
  nav?: React.ReactNode;
  /**
   * Pinned toolbar row below `nav` (search, filters, primary actions). Panels can also portal into it with
   * `PageActionsPortal`. Collapses on scroll down when the pinned chrome exceeds 25% of the viewport.
   */
  actions?: React.ReactNode;
  /** Page content; this is the single scroll region of the page column. */
  children: React.ReactNode;
  /** Extra classes for the frame root. */
  className?: string;
  /** Extra classes for the body (layout of the content, never scrollbar styling). */
  bodyClassName?: string;
  /** Optional ref to the body element (scroll-to-top, measuring). */
  bodyRef?: MutableRefObject<HTMLDivElement | null>;
  /** Accessible name for the body region when it is worth landing on. */
  ariaLabel?: string;
  /**
   * Default true: the body scrolls its own content. Pass false when a child owns the scroller (a nested PageFrame, a
   * log list); the body then clips and lays its children out as a flex column that fills it. A non-scrolling frame
   * never collapses its actions; the nested frame owns that.
   */
  scroll?: boolean;
  /**
   * On `md:` and up, render `nav` and `actions` side by side in one row (nav keeps its width, actions fill the rest).
   * On mobile they stay stacked and the actions row stays collapsible.
   */
  inlineActions?: boolean;
}

/**
 * Viewport-fit page column: `nav` and `actions` stay pinned, the body takes the rest of the height and is the only
 * thing that scrolls. Place it as the direct child of `main` (or of another frame's non-scrolling body).
 */
export const PageFrame: React.FC<PageFrameProps> = ({
  nav,
  actions,
  children,
  className = '',
  bodyClassName = '',
  bodyRef,
  ariaLabel,
  scroll = true,
  inlineActions = false,
}) => {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const navRef = useRef<HTMLDivElement | null>(null);
  const actionsWrapRef = useRef<HTMLDivElement | null>(null);
  const actionsInnerRef = useRef<HTMLDivElement | null>(null);
  const localBodyRef = useRef<HTMLDivElement | null>(null);
  const [slot, setSlot] = useState<HTMLDivElement | null>(null);

  const setBody = useCallback(
    (el: HTMLDivElement | null): void => {
      localBodyRef.current = el;
      if (bodyRef) bodyRef.current = el;
    },
    [bodyRef]
  );

  const { collapsed, reveal } = useCollapsibleChrome({
    root: rootRef,
    nav: navRef,
    actionsInner: actionsInnerRef,
    actionsWrap: actionsWrapRef,
  });

  const hasNav = nav !== undefined && nav !== null && nav !== false;
  const hasActions = actions !== undefined && actions !== null && actions !== false;

  return (
    <PageActionsSlotContext.Provider value={slot}>
      <div
        ref={rootRef}
        data-page-frame=""
        className={`flex flex-col flex-1 min-h-0 min-w-0 ${className}`}
      >
        <div className={inlineActions ? 'shrink-0 flex flex-col md:flex-row md:items-stretch md:gap-2' : 'contents'}>
          {hasNav && (
            <div ref={navRef} className={`shrink-0 space-y-2 pb-2 ${inlineActions ? 'md:min-w-0 md:shrink-0' : ''}`}>
              {nav}
            </div>
          )}
          {/* Focus moving into the collapsed row (keyboard) reveals it; the row stays focusable while hidden. */}
          <div
            ref={actionsWrapRef}
            onFocusCapture={collapsed ? reveal : undefined}
            className={`shrink-0 grid motion-reduce:transition-none transition-[grid-template-rows] duration-150 ease-out ${
              inlineActions ? 'md:flex-1 md:min-w-0' : ''
            } ${collapsed ? 'grid-rows-[0fr]' : 'grid-rows-[1fr]'}`}
          >
            <div ref={actionsInnerRef} className="min-h-0 overflow-hidden">
              {hasActions && <div className="pb-2">{actions}</div>}
              <div ref={setSlot} className="pb-2 empty:hidden" />
            </div>
          </div>
        </div>
        <div
          ref={setBody}
          data-page-body=""
          aria-label={ariaLabel}
          className={`flex-1 min-h-0 ${
            scroll ? 'overflow-y-auto overscroll-y-contain' : 'flex flex-col overflow-hidden'
          } ${bodyClassName}`}
        >
          {children}
        </div>
      </div>
    </PageActionsSlotContext.Provider>
  );
};
