import React, { useId } from 'react';

export interface SelectionCheckboxProps {
  checked: boolean;
  /** Locked while a server-side "select all" is active (the server takes no per-row exclusions). */
  disabled?: boolean;
  label: string;
  /** Form name; defaults to `select-item`. */
  name?: string;
  onChange: () => void;
  /** Flow with the layout (rows) instead of overlaying a tile corner. */
  inline?: boolean;
  className?: string;
}

/** 44px touch target checkbox. Overlays a tile's top-left corner (parent must be `relative`) unless `inline`. */
export const SelectionCheckbox: React.FC<SelectionCheckboxProps> = ({
  checked,
  disabled = false,
  label,
  name = 'select-item',
  onChange,
  inline = false,
  className = '',
}) => {
  const id = useId();
  return (
  <label
    onClick={(e) => e.stopPropagation()}
    className={`${inline ? 'relative shrink-0' : 'absolute top-1 left-1 z-10'} flex h-9 w-9 items-center justify-center rounded-[3px] border border-[#2a2a2a] bg-black/70 ${
      disabled ? 'cursor-not-allowed' : 'cursor-pointer'
    } ${className}`}
  >
    <input
      id={id}
      name={name}
      type="checkbox"
      checked={checked}
      disabled={disabled}
      onChange={onChange}
      aria-label={label}
      className="h-5 w-5 accent-[#e5a00d] cursor-pointer disabled:cursor-not-allowed"
    />
  </label>
  );
};
