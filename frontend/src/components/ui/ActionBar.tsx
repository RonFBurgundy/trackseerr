import React from 'react';

export interface ActionBarProps extends React.HTMLAttributes<HTMLDivElement> {
  children: React.ReactNode;
  /** Horizontal alignment from `sm` up (on mobile the bar always fills the width). */
  align?: 'start' | 'end';
  /** One button per row on mobile instead of an equal-width grid. */
  stackOnMobile?: boolean;
  /** Seat the buttons in a recessed transport bay. */
  bay?: boolean;
  className?: string;
}

/**
 * Row of related buttons. On mobile: an equal-width grid of same-height keys
 * (primary action first). From `sm` up: a wrapping row. Use instead of ad hoc
 * `flex flex-wrap` button rows.
 */
export const ActionBar: React.FC<ActionBarProps> = ({
  children,
  align = 'start',
  stackOnMobile = false,
  bay = false,
  className = '',
  ...props
}) => (
  <div
    className={`action-bar ${bay ? 'tape-transport-bay' : ''} ${className}`}
    data-align={align}
    data-span={stackOnMobile ? 'single' : 'auto'}
    {...props}
  >
    {children}
  </div>
);
