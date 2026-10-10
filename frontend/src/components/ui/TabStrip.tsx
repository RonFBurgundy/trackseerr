import React, { useEffect, useRef } from 'react';
import { TapeTransportBay } from './TapeTransportBay';

export interface TabStripProps extends React.HTMLAttributes<HTMLDivElement> {
  children: React.ReactNode;
  /** Keys share spare width equally on narrow screens (they still scroll if their labels do not fit). */
  fill?: boolean;
  className?: string;
}

/**
 * Recessed bay holding a row of tab keys. Never wraps: on narrow screens it
 * scrolls horizontally and keeps the engaged key in view; from `sm` up the keys
 * sit in a normal row. Pass TapeDeckButton children of one size so the row is
 * uniform in height.
 */
export const TabStrip: React.FC<TabStripProps> = ({
  children,
  fill = false,
  className = '',
  onPointerDown,
  onTouchStart,
  onScroll,
  ...props
}) => {
  const ref = useRef<HTMLDivElement>(null);
  const lastCentredRef = useRef<HTMLElement | null>(null);
  const userInteractingUntilRef = useRef<number>(0);

  const markUserInteraction = () => {
    userInteractingUntilRef.current = Date.now() + 800;
  };

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    markUserInteraction();
    onPointerDown?.(e);
  };

  const handleTouchStart = (e: React.TouchEvent<HTMLDivElement>) => {
    markUserInteraction();
    onTouchStart?.(e);
  };

  const handleScroll = (e: React.UIEvent<HTMLDivElement>) => {
    markUserInteraction();
    onScroll?.(e);
  };

  useEffect(() => {
    const strip = ref.current;
    if (!strip) return;
    const engaged = strip.querySelector<HTMLElement>('.engaged, [aria-current="page"], [aria-selected="true"]');
    if (!engaged) return;

    if (engaged !== lastCentredRef.current) {
      const isFirstMount = lastCentredRef.current === null;
      lastCentredRef.current = engaged;

      if (!isFirstMount && Date.now() < userInteractingUntilRef.current) {
        return;
      }

      const left = engaged.offsetLeft - (strip.clientWidth - engaged.offsetWidth) / 2;
      strip.scrollTo({
        left: Math.max(0, left),
        behavior: isFirstMount ? 'instant' : 'auto',
      });
    }
  });

  return (
    <TapeTransportBay
      ref={ref}
      className={`tab-strip ${className}`}
      data-fill={fill ? 'true' : 'false'}
      onPointerDown={handlePointerDown}
      onTouchStart={handleTouchStart}
      onScroll={handleScroll}
      {...props}
    >
      {children}
    </TapeTransportBay>
  );
};

