import React, { useRef, type MutableRefObject } from 'react';
import { useFillViewportHeight } from '@/hooks/useFillViewportHeight';

export interface ScrollFillProps {
  children: React.ReactNode;
  className?: string;
  /** Optional ref to the scrolling element (auto-scroll, jump-to). */
  scrollRef?: MutableRefObject<HTMLDivElement | null>;
  /** Accessible name when the region is a log or list worth landing on. */
  ariaLabel?: string;
}

/**
 * A panel body that ends at the bottom of the visible area and scrolls its own content, so the page never grows past
 * the viewport. Place it as the last block of a page; everything above it keeps its natural height.
 */
export const ScrollFill: React.FC<ScrollFillProps> = ({ children, className = '', scrollRef, ariaLabel }) => {
  const ownRef = useRef<HTMLDivElement | null>(null);
  const ref = scrollRef ?? ownRef;
  const height = useFillViewportHeight(ref);
  return (
    <div
      ref={ref}
      aria-label={ariaLabel}
      className={`overflow-y-auto overscroll-y-contain ${className}`}
      style={height === null ? undefined : { height }}
    >
      {children}
    </div>
  );
};
