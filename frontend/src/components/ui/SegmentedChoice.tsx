import React, { useRef } from 'react';
import { TapeDeckButton } from './TapeDeckButton';

export interface SegmentedChoiceOption {
  id: string;
  label: string;
}

export interface SegmentedChoiceProps {
  options: ReadonlyArray<SegmentedChoiceOption>;
  value: string;
  onChange: (id: string) => void;
  ariaLabel: string;
  name: string;
  disabled?: boolean;
  className?: string;
}

/**
 * SegmentedChoice: a `role="radiogroup"` row of `TapeDeckButton size="sm"` using `active`
 * for the selected one (`role="radio"`, `aria-checked`), with arrow-key navigation between options.
 * Wraps on narrow screens.
 */
export const SegmentedChoice: React.FC<SegmentedChoiceProps> = ({
  options,
  value,
  onChange,
  ariaLabel,
  name,
  disabled = false,
  className = '',
}) => {
  const containerRef = useRef<HTMLDivElement>(null);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (disabled || options.length === 0) return;

    const enabledOptions = options;
    const currentIndex = enabledOptions.findIndex((opt) => opt.id === value);

    let nextIndex = -1;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
      e.preventDefault();
      nextIndex = currentIndex < 0 ? 0 : (currentIndex + 1) % enabledOptions.length;
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
      e.preventDefault();
      nextIndex = currentIndex < 0 ? 0 : (currentIndex - 1 + enabledOptions.length) % enabledOptions.length;
    } else if (e.key === 'Home') {
      e.preventDefault();
      nextIndex = 0;
    } else if (e.key === 'End') {
      e.preventDefault();
      nextIndex = enabledOptions.length - 1;
    }

    if (nextIndex >= 0) {
      const nextOpt = enabledOptions[nextIndex];
      onChange(nextOpt.id);
      // Focus the button corresponding to the next option
      const buttons = containerRef.current?.querySelectorAll<HTMLButtonElement>('button[role="radio"]');
      if (buttons && buttons[nextIndex]) {
        buttons[nextIndex].focus();
      }
    }
  };

  return (
    <div
      ref={containerRef}
      role="radiogroup"
      aria-label={ariaLabel}
      onKeyDown={handleKeyDown}
      className={`flex flex-wrap items-center gap-1.5 sm:gap-2 ${className}`}
    >
      {options.map((opt) => {
        const isChecked = opt.id === value;
        return (
          <TapeDeckButton
            key={opt.id}
            role="radio"
            aria-checked={isChecked}
            tabIndex={isChecked ? 0 : -1}
            size="sm"
            active={isChecked}
            disabled={disabled}
            name={name}
            onClick={() => {
              if (!disabled && opt.id !== value) {
                onChange(opt.id);
              }
            }}
          >
            {opt.label}
          </TapeDeckButton>
        );
      })}
    </div>
  );
};
