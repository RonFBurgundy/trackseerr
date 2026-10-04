import React from 'react';

export interface TapeDeckButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  active?: boolean;
  variant?: 'default' | 'danger' | 'amber';
  size?: 'sm' | 'md' | 'lg';
  icon?: React.ReactNode;
  /** Icon-only below `sm` (the label stays in the DOM for assistive tech via aria-label). Needs `icon`. */
  collapseLabel?: boolean | 'sm' | 'lg' | 'xl';
  children?: React.ReactNode;
}

const COLLAPSE_CLASS = { sm: 'hidden sm:inline', lg: 'hidden lg:inline', xl: 'hidden xl:inline' } as const;

export const TapeDeckButton: React.FC<TapeDeckButtonProps> = ({
  active = false,
  variant = 'default',
  size = 'md',
  icon,
  collapseLabel = false,
  children,
  className = '',
  disabled = false,
  ...props
}) => {
  const sizeClasses = {
    sm: 'text-xs px-2.5 py-1.5 min-h-[44px] sm:min-h-[32px]',
    md: 'text-xs sm:text-sm px-3.5 py-2 min-h-[44px] sm:min-h-[38px]',
    lg: 'text-sm sm:text-base px-5 py-2.5 min-h-[44px] sm:min-h-[44px]',
  }[size];

  const variantClasses = {
    default: active
      ? 'engaged text-white border-[#e5a00d]/40'
      : 'text-neutral-300 hover:text-white',
    danger: active
      ? 'engaged text-red-400 border-red-500/50'
      : 'text-red-400 hover:text-red-300 border-red-900/50 hover:border-red-700/60',
    amber: active
      ? 'engaged text-[#e5a00d] border-[#e5a00d]'
      : 'text-[#e5a00d] hover:text-[#f5b82e] border-[#e5a00d]/40 hover:border-[#e5a00d]/80',
  }[variant];

  return (
    <button
      className={`tape-deck-btn relative inline-flex items-center justify-center gap-2 font-semibold uppercase tracking-wider select-none rounded-[3px] transition-all duration-75 active:translate-y-[2px] ${sizeClasses} ${variantClasses} ${
        active ? 'engaged' : ''
      } ${className}`}
      disabled={disabled}
      aria-label={props['aria-label'] ?? (collapseLabel && typeof children === 'string' ? children : undefined)}
      {...props}
    >
      {/* 2px amber LED indicator jewel when active/engaged */}
      {active && (
        <span
          className="tape-deck-indicator absolute top-0 left-[15%] right-[15%] h-[2px] bg-[#e5a00d] shadow-[0_0_6px_rgba(229,160,13,0.8)] rounded-[1px]"
          aria-hidden="true"
        />
      )}
      {icon && <span className="inline-flex shrink-0 items-center justify-center">{icon}</span>}
      {children && <span className={collapseLabel && icon ? COLLAPSE_CLASS[collapseLabel === true ? 'sm' : collapseLabel] : undefined}>{children}</span>}
    </button>
  );
};
