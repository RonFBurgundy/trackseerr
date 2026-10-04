import React, { useEffect, useState } from 'react';
import { TapeDeckButton } from './TapeDeckButton';

export interface ConfirmDangerButtonProps {
  /** Runs after the second click; the button disarms immediately. */
  onConfirm: () => void;
  icon: React.ReactNode;
  /** Accessible name for the idle (first-step) button. */
  ariaLabel: string;
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
      />
    );
  }

  return (
    <>
      <TapeDeckButton
        size={size}
        variant="danger"
        disabled={disabled}
        onClick={() => {
          setArmed(false);
          onConfirm();
        }}
      >
        {confirmLabel}
      </TapeDeckButton>
      <TapeDeckButton size={size} onClick={() => setArmed(false)}>
        {cancelLabel}
      </TapeDeckButton>
    </>
  );
};
