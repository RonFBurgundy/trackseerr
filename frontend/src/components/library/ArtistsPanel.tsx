import React, { useCallback, useEffect } from 'react';
import { CheckSquare } from 'lucide-react';
import type { ArtistItem } from '@/types/models';
import { getArtistsIndex, getArtistsPaged } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';
import { pagedFetcher } from '@/hooks/useVirtualPagedList';
import { useLibraryCatalog, type LibrarySortOption } from '@/hooks/useLibraryCatalog';
import { useMonitoredOverrides } from '@/hooks/useMonitoredOverrides';
import { useBulkSelection } from '@/hooks/useBulkSelection';
import { useArtistBulkEdit } from '@/hooks/useArtistBulkEdit';
import { useQualityProfiles } from '@/hooks/useQualityProfiles';
import { TapeDeckButton } from '@/components/ui';
import { ListPanel, ScrubberRail, VirtualGrid } from '@/components/lists';
import { LibrarySortControl } from './LibrarySortControl';
import { ArtistTile } from './ArtistTile';
import { ArtistBulkBar } from './ArtistBulkBar';

const SORT_OPTIONS: ReadonlyArray<LibrarySortOption> = [
  { key: 'name', label: 'Name', defaultDir: 'asc' },
  { key: 'added_at', label: 'Added', defaultDir: 'desc' },
  { key: 'album_count', label: 'Albums', defaultDir: 'desc' },
];

const fetchArtists = pagedFetcher<ArtistItem>(getArtistsPaged);
const getKey = (a: ArtistItem): string | number => a.id;

export interface ArtistsPanelProps {
  query: string;
  monitoredOnly: boolean;
  isAdmin: boolean;
  /** Changes when the catalog was rebuilt (scan finished); the list reloads. */
  reloadToken: number;
  onOpenArtist: (artistId: number | string) => void;
  onToggleMonitored: (artistId: number | string, monitored: boolean) => Promise<void>;
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
  onToggleMonitored,
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
  const overrides = useMonitoredOverrides();
  const { refresh, reload, mode } = list;

  // Bulk editing is a native-library, admin-only action; Lidarr owns monitoring in its own mode.
  const canBulkEdit = isAdmin && mode !== 'lidarr';
  const selection = useBulkSelection();
  const { toggle: toggleSelected, isSelected, allMatching, active: selecting, exit: exitSelection } = selection;
  const bulk = useArtistBulkEdit(selection, list.total, onToast, reload);
  const profiles = useQualityProfiles(selecting, onToast);
  const unfiltered = !query && !monitoredOnly;

  // Filters change what "all" would mean in the user's head; drop the selection rather than act on a stale view.
  useEffect(() => {
    exitSelection();
  }, [query, monitoredOnly, exitSelection]);

  useEffect(() => onModeChange(mode), [mode, onModeChange]);
  useEffect(() => {
    if (reloadToken > 0) reload();
  }, [reloadToken, reload]);

  const handleToggle = useCallback(
    async (artistId: number | string, next: boolean): Promise<void> => {
      overrides.set(artistId, next);
      try {
        await onToggleMonitored(artistId, next);
        await refresh();
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to update monitoring'), 'error');
      } finally {
        overrides.clear(artistId);
      }
    },
    [overrides, onToggleMonitored, refresh, onToast]
  );

  const renderItem = useCallback(
    (artist: ArtistItem): React.ReactNode => (
      <ArtistTile
        artist={artist}
        monitored={overrides.resolve(artist.id, artist.monitored)}
        isAdmin={isAdmin}
        onOpen={onOpenArtist}
        onToggleMonitored={(id, val) => void handleToggle(id, val)}
        selection={
          selecting
            ? { checked: isSelected(artist.id), locked: allMatching, onToggle: () => toggleSelected(artist.id) }
            : undefined
        }
      />
    ),
    [overrides, isAdmin, onOpenArtist, handleToggle, selecting, isSelected, allMatching, toggleSelected]
  );

  return (
    <ListPanel
      title="Artists"
      mode={mode}
      total={list.total}
      toolbar={
        <>
          {canBulkEdit && (
            <TapeDeckButton
              size="sm"
              active={selecting}
              aria-pressed={selecting}
              icon={<CheckSquare className="h-3.5 w-3.5" />}
              onClick={selecting ? exitSelection : selection.enter}
            >
              Select
            </TapeDeckButton>
          )}
          <LibrarySortControl options={SORT_OPTIONS} sortKey={sortKey} sortDir={sortDir} onChange={changeSort} />
        </>
      }
    >
      {canBulkEdit && selecting && (
        <ArtistBulkBar
          selection={selection}
          total={list.total}
          canSelectAll={unfiltered}
          edit={bulk}
          profiles={profiles.profiles}
          profilesLoading={profiles.loading}
        />
      )}
      <VirtualGrid<ArtistItem>
        list={list}
        getKey={getKey}
        renderItem={renderItem}
        captionHeight={89}
        emptyMessage={query ? 'No artists match your search.' : 'No artists found in library.'}
        ariaLabel="Artists"
        rail={<ScrubberRail groups={index.groups} ariaLabel="Jump to group" />}
      />
    </ListPanel>
  );
};
