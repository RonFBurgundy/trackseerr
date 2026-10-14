import React from 'react';

export interface RangeSliderProps {
  id: string;
  name: string;
  min: number;
  max: number;
  step?: number;
  value: number;
  onChange: (value: number) => void;
  onCommit?: (value: number) => void;
  disabled?: boolean;
  ariaLabel: string;
  ariaValueText?: string;
  className?: string;
}

/**
 * Single-handle slider wrapping one labelled `<input type="range">`.
 * Adheres to TrackSeerr tactile design: 32px touch area on mobile, accent-amber.
 */
export const RangeSlider: React.FC<RangeSliderProps> = ({
  id,
  name,
  min,
  max,
  step = 1,
  value,
  onChange,
  onCommit,
  disabled = false,
  ariaLabel,
  ariaValueText,
  className = '',
}) => {
  return (
    <input
      id={id}
      name={name}
      type="range"
      min={min}
      max={max}
      step={step}
      value={value}
      disabled={disabled}
      aria-label={ariaLabel}
      aria-valuetext={ariaValueText}
      onChange={(e) => onChange(Number(e.target.value))}
      onPointerUp={() => onCommit?.(value)}
      onKeyUp={(e) => {
        if (e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'ArrowUp' || e.key === 'ArrowDown' || e.key === 'Home' || e.key === 'End' || e.key === 'PageUp' || e.key === 'PageDown') {
          onCommit?.(value);
        }
      }}
      className={`w-full h-8 sm:h-6 accent-[var(--accent-amber)] ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'} ${className}`}
    />
  );
};
