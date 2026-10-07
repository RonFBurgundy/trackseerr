import React from 'react';

export interface ArtistNameLinkProps {
  name: string | null | undefined;
  /** When absent the artist has no profile and the name renders as plain text. */
  discoveryId?: string | null;
  onOpen: (discoveryId: string) => void;
  className?: string;
}

/** Artist name that opens the artist profile. Stops propagation so a surrounding card click does not also fire. */
export const ArtistNameLink: React.FC<ArtistNameLinkProps> = ({ name, discoveryId, onOpen, className = '' }) => {
  if (!discoveryId) {
    return (
      <span className={`truncate ${className}`} title={name ?? undefined}>
        {name}
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onOpen(discoveryId);
      }}
      className={`max-w-full truncate text-left hover:text-[#e5a00d] hover:underline focus:text-[#e5a00d] focus:outline-none ${className}`}
      title={`Open ${name}`}
    >
      {name}
    </button>
  );
};
