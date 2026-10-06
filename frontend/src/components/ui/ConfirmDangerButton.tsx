import React, { useEffect, useState } from 'react';
import { TapeDeckButton } from './TapeDeckButton';

export interface ConfirmDangerButtonProps {
  /** Runs after the second click; the button disarms immediately. */
  onConfirm: () => void;
  icon: React.ReactNode;
  /** Accessible name for the idle (first-step) button. */
  ariaLabel: string;
  /** Visible text for the idle button (icon-only when omitted). */
  idleLabel?: string;
  /** Hide the idle label below `sm` (icon-only on phones); the aria-label still names it. */
  collapseLabel?: boolean;
  confirmLabel?: string;
  cancelLabel?: string;
  size?: 'sm' | 'md' | 'lg';
  disabled?: boolean;
  /** Disarms on its own after this many ms so a stray armed button never lingers. */
  autoDisarmMs?: number;
}

/** Inline two-step destructive action (replaces window.confirm): first click arms, second click confirms. */
export const ConfirmDangerButton: React.FC<ConfirmDangerButtonProps> = ({
  onConfirm,
  icon,
  ariaLabel,
  idleLabel,
  collapseLabel = false,
  confirmLabel = 'Confirm',
  cancelLabel = 'Keep',
  size = 'sm',
  disabled = false,
  autoDisarmMs = 6000,
}) => {
  const [armed, setArmed] = useState<boolean>(false);

  useEffect(() => {
    if (!armed) return undefined;
    const timer = window.setTimeout(() => setArmed(false), autoDisarmMs);
    return () => window.clearTimeout(timer);
  }, [armed, autoDisarmMs]);

  if (!armed) {
    return (
      <TapeDeckButton
        size={size}
        variant="danger"
        disabled={disabled}
        onClick={() => setArmed(true)}
        icon={icon}
        aria-label={ariaLabel}
        title={ariaLabel}
        collapseLabel={collapseLabel}
      >
        {idleLabel}
      </TapeDeckButton>
    );
  }

  return (
    <div className="col-span-full flex items-stretch gap-2 sm:contents">
      <TapeDeckButton
        size={size}
        variant="danger"
        className="min-w-0 flex-1 sm:flex-none"
        disabled={disabled}
        onClick={() => {
          setArmed(false);
          onConfirm();
        }}
      >
        {confirmLabel}
      </TapeDeckButton>
      <TapeDeckButton size={size} className="shrink-0" onClick={() => setArmed(false)}>
        {cancelLabel}
      </TapeDeckButton>
    </div>
  );
};
