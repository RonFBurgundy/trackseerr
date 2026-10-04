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
export const TabStrip: React.FC<TabStripProps> = ({ children, fill = false, className = '', ...props }) => {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const strip = ref.current;
    if (!strip) return;
    const engaged = strip.querySelector<HTMLElement>('.engaged, [aria-current="page"], [aria-selected="true"]');
    if (!engaged) return;
    const left = engaged.offsetLeft - (strip.clientWidth - engaged.offsetWidth) / 2;
    strip.scrollTo({ left: Math.max(0, left) });
  });

  return (
    <TapeTransportBay ref={ref} className={`tab-strip ${className}`} data-fill={fill ? 'true' : 'false'} {...props}>
      {children}
    </TapeTransportBay>
  );
};
