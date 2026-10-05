import React, { useId } from 'react';

interface TactileSwitchBaseProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  className?: string;
  title?: string;
  /** Overrides the generated id. */
  id?: string;
  /** Form name on the switch button. */
  name?: string;
}

/**
 * Either a visible `label` or an `ariaLabel` is required. Per-row switches should pass an `ariaLabel`
 * that names the row subject (e.g. "Monitor Track Title").
 */
export type TactileSwitchProps = TactileSwitchBaseProps &
  ({ label: string; ariaLabel?: string } | { label?: undefined; ariaLabel: string });

export const TactileSwitch: React.FC<TactileSwitchProps> = ({
  checked,
  onChange,
  label,
  ariaLabel,
  disabled = false,
  className = '',
  title,
  id,
  name,
}) => {
  const generatedId = useId();
  const switchId = id ?? generatedId;
  const accessibleName = ariaLabel ?? label;
  return (
    <label
      title={title}
      className={`inline-flex items-center gap-2 sm:gap-3 select-none cursor-pointer ${
        disabled ? 'opacity-50 cursor-not-allowed' : ''
      } ${className}`}
    >
      <button
        id={switchId}
        name={name ?? `switch-${switchId.replace(/:/g, '')}`}
        aria-label={accessibleName}
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => !disabled && onChange(!checked)}
        className="group relative inline-flex h-10 w-11 shrink-0 items-center justify-center focus:outline-none max-sm:-my-2 sm:h-6"
      >
        <span
          className={`tactile-switch relative inline-flex h-6 w-11 shrink-0 items-center rounded-[4px] border border-[#222222] bg-[#0d0d0d] p-0.5 transition-colors duration-100 ease-in-out group-focus-visible:border-[#e5a00d] ${
            checked ? 'border-[#e5a00d]/50 bg-[#151515]' : ''
          }`}
        >
          <span
            className={`pointer-events-none inline-block h-4 w-5 transform rounded-[2px] border border-[#383838] bg-gradient-to-b from-[#2a2a2a] to-[#1c1c1c] shadow-[0_1px_3px_rgba(0,0,0,0.5)] transition-transform duration-100 ease-in-out ${
              checked ? 'translate-x-5 border-[#e5a00d]/80 bg-gradient-to-b from-[#e5a00d] to-[#c78605] shadow-[0_0_6px_rgba(229,160,13,0.5)]' : 'translate-x-0'
            }`}
          >
            {/* Subtle ribbed grip texture */}
            <span className="flex h-full w-full items-center justify-center gap-0.5">
              <span className="h-2 w-[1px] bg-black/40" />
              <span className="h-2 w-[1px] bg-black/40" />
              <span className="h-2 w-[1px] bg-black/40" />
            </span>
          </span>
        </span>
      </button>
      {label && <span className="text-xs uppercase font-medium tracking-wide text-neutral-300">{label}</span>}
    </label>
  );
};
