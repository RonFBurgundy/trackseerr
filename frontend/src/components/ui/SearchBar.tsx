import React, { useId } from 'react';
import { Search, X } from 'lucide-react';

export interface SearchBarProps {
  value: string;
  onChange: (value: string) => void;
  onSearch?: () => void;
  placeholder?: string;
  className?: string;
  disabled?: boolean;
  /** Accessible name (the search box has no visible label). Default: the placeholder. */
  ariaLabel?: string;
  /** Form name. Default `search`. */
  name?: string;
  /** Overrides the generated id. */
  id?: string;
}

export const SearchBar: React.FC<SearchBarProps> = ({
  value,
  onChange,
  onSearch,
  placeholder = 'Search...',
  className = '',
  disabled = false,
  ariaLabel,
  name = 'search',
  id,
}) => {
  const generatedId = useId();
  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && onSearch) {
      onSearch();
    }
  };

  return (
    <div
      className={`tape-transport-bay relative flex items-center w-full min-h-[44px] bg-[#0d0d0d] border border-[#1f1f1f] rounded-[4px] px-3 py-1 shadow-transport-bay ${className}`}
    >
      <Search className="h-4 w-4 text-neutral-400 shrink-0 mr-2.5" />
      <input
        id={id ?? generatedId}
        name={name}
        aria-label={ariaLabel ?? placeholder}
        autoComplete="off"
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        disabled={disabled}
        className="w-full bg-transparent text-sm text-white placeholder-neutral-500 focus:outline-none"
      />
      {value && (
        <button
          type="button"
          onClick={() => onChange('')}
          disabled={disabled}
          className="p-1 text-neutral-400 hover:text-white transition-colors duration-75 shrink-0"
          aria-label="Clear search"
        >
          <X className="h-4 w-4" />
        </button>
      )}
    </div>
  );
};
