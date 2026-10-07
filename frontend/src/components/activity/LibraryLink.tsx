import React from 'react';
import { routeToHash } from '@/hooks/useAppRoute';
import { orDash } from '@/components/lists';

export interface LibraryLinkProps {
  kind: 'artist' | 'album';
  /** Library id (Lidarr id in Lidarr mode, native id otherwise); without it the text renders unlinked. */
  id: string | null | undefined;
  label: string | null | undefined;
}

/** Name that deep-links to the library artist / album view when its id is known. */
export const LibraryLink: React.FC<LibraryLinkProps> = ({ kind, id, label }) => {
  if (!id || !label || !label.trim()) return <>{orDash(label)}</>;
  const href =
    kind === 'artist'
      ? routeToHash({ tab: 'library', sub: 'artists', detail: { artistId: id } })
      : routeToHash({ tab: 'library', sub: 'albums', detail: { albumId: id } });
  return (
    <a href={href} className="hover:text-[var(--accent-amber)] hover:underline" title={`Open ${kind} in library`}>
      {label}
    </a>
  );
};
