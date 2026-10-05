import React, { useCallback, useEffect } from 'react';
import type { ArtistItem } from '@/types/models';
import { getArtistsIndex, getArtistsPaged } from '@/services/libraryService';
import { pagedFetcher } from '@/hooks/useVirtualPagedList';
import { useLibraryCatalog, type LibrarySortOption } from '@/hooks/useLibraryCatalog';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import { useBulkSelection } from '@/hooks/useBulkSelection';
import { useArtistBulkEdit } from '@/hooks/useArtistBulkEdit';
import { useQualityProfiles } from '@/hooks/useQualityProfiles';
import { useReleaseProfiles } from '@/hooks/useReleaseProfiles';
import { ScrubberRail, VirtualGrid } from '@/components/lists';
import { LibrarySortControl } from './LibrarySortControl';
import { LibrarySelectKey, LibraryToolbarPortal } from './LibraryToolbarPortal';
import { ArtistTile } from './ArtistTile';
import { ArtistBulkBar } from './ArtistBulkBar';

const SORT_OPTIONS: ReadonlyArray<LibrarySortOption> = [
  { key: 'name', label: 'Name', defaultDir: 'asc' },
  { key: 'added_at', label: 'Added', defaultDir: 'desc' },
  { key: 'album_count', label: 'Albums', defaultDir: 'desc' },
];

/** Exact height under the square art: p-1.5 padding, 16px title, 2px gap, 14px detail line, 2px borders. */
const CAPTION_HEIGHT = 46;

const fetchArtists = pagedFetcher<ArtistItem>(getArtistsPaged);
const getKey = (a: ArtistItem): string | number => a.id;

export interface ArtistsPanelProps {
  query: string;
  monitoredOnly: boolean;
  isAdmin: boolean;
  /** Changes when the catalog was rebuilt (scan finished); the list reloads. */
  reloadToken: number;
  onOpenArtist: (artistId: number | string) => void;
  /** Toolbar slot in the page header row; select and sort render there. */
  toolbarSlot: HTMLElement | null;
  /** Stats footer, rendered after the last row inside the scroll area. */
  footer: React.ReactNode;
  onModeChange: (mode: string | null) => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Artists as a virtualized cover grid with a scrubber; search and sort are server-side. */
export const ArtistsPanel: React.FC<ArtistsPanelProps> = ({
  query,
  monitoredOnly,
  isAdmin,
  reloadToken,
  onOpenArtist,
  toolbarSlot,
  footer,
  onModeChange,
  onToast,
}) => {
  const { list, index, sortKey, sortDir, changeSort } = useLibraryCatalog<ArtistItem>({
    fetchPage: fetchArtists,
    fetchIndex: getArtistsIndex,
    getKey,
    sortOptions: SORT_OPTIONS,
    query,
    monitoredOnly,
  });
  const phone = useMediaQuery('(max-width: 639px)');
  const { reload, mode } = list;

  // Bulk editing is a native-library, admin-only action; Lidarr owns monitoring in its own mode.
  const canBulkEdit = isAdmin && mode !== 'lidarr';
  const selection = useBulkSelection();
  const { toggle: toggleSelected, isSelected, allMatching, active: selecting, exit: exitSelection } = selection;
  const bulk = useArtistBulkEdit(selection, list.total, onToast, reload);
  const profiles = useQualityProfiles(selecting, onToast);
  const releaseProfiles = useReleaseProfiles(selecting && canBulkEdit, onToast);
  const unfiltered = !query && !monitoredOnly;

  // Filters change what "all" would mean in the user's head; drop the selection rather than act on a stale view.
  useEffect(() => {
    exitSelection();
  }, [query, monitoredOnly, exitSelection]);

  useEffect(() => onModeChange(mode), [mode, onModeChange]);
  useEffect(() => {
    if (reloadToken > 0) reload();
  }, [reloadToken, reload]);

  const renderItem = useCallback(
    (artist: ArtistItem): React.ReactNode => (
      <ArtistTile
        artist={artist}
        monitored={artist.monitored}
        onOpen={onOpenArtist}
        selection={
          selecting
            ? { checked: isSelected(artist.id), locked: allMatching, onToggle: () => toggleSelected(artist.id) }
            : undefined
        }
      />
    ),
    [onOpenArtist, selecting, isSelected, allMatching, toggleSelected]
  );

  return (
    <section className="flex min-h-0 flex-col gap-2" aria-label="Artists">
      <LibraryToolbarPortal slot={toolbarSlot}>
        {canBulkEdit && <LibrarySelectKey active={selecting} onToggle={selecting ? exitSelection : selection.enter} />}
        <LibrarySortControl options={SORT_OPTIONS} sortKey={sortKey} sortDir={sortDir} onChange={changeSort} />
      </LibraryToolbarPortal>
      {canBulkEdit && selecting && (
        <ArtistBulkBar
          selection={selection}
          total={list.total}
          canSelectAll={unfiltered}
          edit={bulk}
          profiles={profiles.profiles}
          profilesLoading={profiles.loading}
          releaseProfiles={releaseProfiles.profiles}
          releaseProfilesLoading={releaseProfiles.loading}
        />
      )}
      <VirtualGrid<ArtistItem>
        list={list}
        getKey={getKey}
        renderItem={renderItem}
        fixedColumns={phone ? 3 : undefined}
        minTileWidth={128}
        gap={phone ? 8 : 12}
        captionHeight={CAPTION_HEIGHT}
        emptyMessage={query ? 'No artists match your search.' : 'No artists found in library.'}
        ariaLabel="Artists"
        rail={<ScrubberRail groups={index.groups} ariaLabel="Jump to group" />}
        footer={footer}
      />
    </section>
  );
};
