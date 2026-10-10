import React, { useEffect, useState } from 'react';
import type { Schema } from '@/types';
import type { LibraryFilters } from '@/types/libraryFilters';
import { EMPTY_LIBRARY_FILTERS } from '@/lib/libraryFilters';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import { LibraryFilterFields } from './LibraryFilterFields';

export interface LibraryFilterSheetProps {
  isOpen: boolean;
  onClose: () => void;
  filters: LibraryFilters;
  onApply: (next: LibraryFilters) => void;
  facets: Schema<'LibraryFacetsResponse'> | null;
  tags?: readonly Schema<'TagOut'>[];
}

export const LibraryFilterSheet: React.FC<LibraryFilterSheetProps> = ({
  isOpen,
  onClose,
  filters,
  onApply,
  facets,
  tags,
}) => {
  const [draft, setDraft] = useState<LibraryFilters>(filters);

  useEffect(() => {
    if (isOpen) {
      setDraft(filters);
    }
  }, [isOpen, filters]);

  const handleClear = (): void => {
    setDraft(EMPTY_LIBRARY_FILTERS);
  };

  const handleApply = (): void => {
    onApply(draft);
    onClose();
  };

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="Library Filters"
      subtitle="Filter library by genre, era, origin, and attributes"
      maxWidth="sm:max-w-2xl"
      footer={
        <div className="flex w-full items-center justify-between gap-2">
          <TapeDeckButton size="sm" onClick={handleClear}>
            Clear
          </TapeDeckButton>
          <div className="flex items-center gap-2">
            <TapeDeckButton size="sm" onClick={onClose}>
              Cancel
            </TapeDeckButton>
            <TapeDeckButton size="sm" variant="amber" active onClick={handleApply}>
              Apply
            </TapeDeckButton>
          </div>
        </div>
      }
    >
      <LibraryFilterFields
        filters={draft}
        onChange={setDraft}
        facets={facets}
        tags={tags}
      />
    </ObsidianModal>
  );
};
