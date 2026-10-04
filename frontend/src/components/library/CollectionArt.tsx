import React from 'react';
import { Disc, Layers } from 'lucide-react';
import type { CollectionItem } from '@/types/models';

export interface CollectionArtProps {
  collection: CollectionItem;
  /** Tailwind classes for the empty-state icon. */
  iconClass: string;
  gridBg?: string;
}

/** Poster, 2x2 cover collage, or placeholder icon, filling its parent box. */
export const CollectionArt: React.FC<CollectionArtProps> = ({ collection, iconClass, gridBg = 'bg-[#141414]' }) => {
  if (collection.poster_url) {
    return <img src={collection.poster_url} alt={collection.name} className="w-full h-full object-cover" loading="lazy" />;
  }
  const covers = collection.preview_covers ?? [];
  if (covers.length > 0) {
    return (
      <div className={`w-full h-full grid grid-cols-2 gap-0.5 p-0.5 ${gridBg}`}>
        {covers.slice(0, 4).map((c, idx) => (
          <img key={idx} src={c} alt="" className="w-full h-full object-cover" />
        ))}
        {Array.from({ length: Math.max(0, 4 - covers.length) }).map((_, idx) => (
          <div key={`empty-${idx}`} className="w-full h-full bg-[#181818] flex items-center justify-center">
            <Disc className="h-4 w-4 text-neutral-700" />
          </div>
        ))}
      </div>
    );
  }
  return <Layers className={iconClass} />;
};
