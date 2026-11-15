import React from 'react';
import { TactileSwitch } from '@/components/ui';
import { PERMISSION_ADMIN, PERMISSION_FLAGS } from '@/types/account';

export interface PermissionSwitchesProps {
  /** Prefix for switch ids: `${idPrefix}-${bit}`. Keeps ids unique per form. */
  idPrefix: string;
  /** Current permission bits. */
  value: number;
  /** Receives the full next permission bits after one switch flips. */
  onChange: (next: number) => void;
  /** Omit the Admin switch entirely (defaults for new users can never grant admin). */
  hideAdmin?: boolean;
  /** Bits shown with their current state but not editable. */
  lockedBits?: number;
  /** Bits shown checked and not editable (e.g. implied by an admin account). */
  allOnBits?: number;
  /** Optional hover text per bit, e.g. explaining why a switch is locked. */
  tooltipFor?: (bit: number) => string | undefined;
}

/** A grid of TactileSwitch rows, one per permission flag. Composes existing widgets only. */
export const PermissionSwitches: React.FC<PermissionSwitchesProps> = ({
  idPrefix,
  value,
  onChange,
  hideAdmin = false,
  lockedBits = 0,
  allOnBits = 0,
  tooltipFor,
}) => (
  <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6">
    {PERMISSION_FLAGS.filter((flag) => !(hideAdmin && flag.bit === PERMISSION_ADMIN)).map((flag) => {
      const isAllOn = (allOnBits & flag.bit) !== 0;
      const isLocked = isAllOn || (lockedBits & flag.bit) !== 0;
      const isChecked = isAllOn || (value & flag.bit) !== 0;
      return (
        <div key={flag.bit} className="flex items-center min-h-[36px] sm:min-h-[32px] text-xs font-mono">
          <TactileSwitch
            id={`${idPrefix}-${flag.bit}`}
            name={`permission-${flag.bit}`}
            checked={isChecked}
            disabled={isLocked}
            onChange={(checked) => onChange(checked ? value | flag.bit : value & ~flag.bit)}
            label={`${flag.label} (${flag.bit})`}
            title={tooltipFor?.(flag.bit)}
          />
        </div>
      );
    })}
  </div>
);
