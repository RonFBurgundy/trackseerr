import React, { useCallback, useEffect, useState } from 'react';
import { Disc, Layers, Loader2, Music, RefreshCw, User } from 'lucide-react';
import type { UseLibraryReturn, LibraryTab } from '@/hooks/useLibrary';
import type { AlbumItem } from '@/types/models';
import { useAddToCollection } from '@/hooks/useAddToCollection';
import { useDebouncedValue } from '@/hooks/useDebouncedValue';
import { useLibraryManager } from '@/hooks/useLibraryManager';
import { useToast } from '@/hooks/useToast';
import { deleteCollection } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';
import { SearchBar, TapeDeckButton, TapeTransportBay, ToastBanner } from '@/components/ui';
import {
  AddToCollectionModal,
  AlbumDetailModal,
  AlbumsPanel,
  ArtistDetail,
  ArtistsPanel,
  CollectionDetail,
  CollectionsPanel,
  LibraryScanBanner,
  LibraryStatsBar,
  LidarrMigrationBanner,
  TracksPanel,
} from '@/components/library';

export interface LibraryViewProps {
  libraryHook: UseLibraryReturn;
  isAdmin?: boolean;
}

const SEARCH_DEBOUNCE_MS = 250;

const TABS: Array<{ id: LibraryTab; label: string; icon: React.ReactNode }> = [
  { id: 'artists', label: 'Artists', icon: <User className="h-3.5 w-3.5" /> },
  { id: 'albums', label: 'Albums', icon: <Disc className="h-3.5 w-3.5" /> },
  { id: 'tracks', label: 'Tracks', icon: <Music className="h-3.5 w-3.5" /> },
  { id: 'collections', label: 'Collections', icon: <Layers className="h-3.5 w-3.5" /> },
];

export const LibraryView: React.FC<LibraryViewProps> = ({ libraryHook, isAdmin = false }) => {
  const {
    activeTab,
    collections,
    stats,
    isScanning,
    scanStatus,
    lidarrStatus,
    isLoading,
    error,
    catalogVersion,
    setTab,
    setSearch,
    triggerScan,
    cancelScan,
    toggleArtistMonitored,
    toggleAlbumMonitored,
    toggleTrackMonitored,
    refresh,
  } = libraryHook;

  const { toast, showToast } = useToast();
  const manager = useLibraryManager(isAdmin);

  const [searchInput, setSearchInput] = useState<string>('');
  const query = useDebouncedValue(searchInput, SEARCH_DEBOUNCE_MS);
  const [monitoredOnly, setMonitoredOnly] = useState<boolean>(false);
  const [selectedArtistId, setSelectedArtistId] = useState<number | string | null>(null);
  const [selectedCollectionId, setSelectedCollectionId] = useState<string | null>(null);
  const [albumForModal, setAlbumForModal] = useState<AlbumItem | null>(null);
  /** Data source reported by the paged artists/albums responses; used until the manager settings load. */
  const [listMode, setListMode] = useState<string | null>(null);

  const lidarrMode = (manager.state?.mode ?? listMode) === 'lidarr';
  // Collections reference native album rows, so adding albums to them is a native-mode action.
  const canCollect = !lidarrMode;

  // Collections search is still resolved by the collections endpoint; the paged lists take `query` directly.
  useEffect(() => {
    setSearch(query);
  }, [query, setSearch]);

  useEffect(() => {
    setSelectedCollectionId(null);
  }, [activeTab, query]);

  // Collections reference native album ids, so the tab only exists when TrackSeerr manages the library.
  const visibleTabs = lidarrMode ? TABS.filter((t) => t.id !== 'collections') : TABS;

  const picker = useAddToCollection(showToast, refresh);

  const handleDeleteCollection = useCallback(
    async (id: string, name: string): Promise<void> => {
      try {
        const ok = await deleteCollection(id);
        if (!ok) {
          showToast('Failed to delete collection', 'error');
          return;
        }
        setSelectedCollectionId((cur) => (cur === id ? null : cur));
        await refresh();
        showToast(`Collection "${name}" deleted`);
      } catch (err: unknown) {
        showToast(errorMessage(err, 'Error deleting collection'), 'error');
      }
    },
    [refresh, showToast]
  );

  const handleCollectionCreated = useCallback(
    async (name: string): Promise<void> => {
      await refresh();
      showToast(`Collection "${name}" created`);
    },
    [refresh, showToast]
  );

  const openCollectPicker = useCallback(
    (album: AlbumItem): void => {
      void picker.open(album);
    },
    [picker]
  );

  const toastNode = toast ? <ToastBanner message={toast.message} tone={toast.tone} /> : null;

  if (selectedArtistId !== null) {
    return (
      <>
        {toastNode}
        <ArtistDetail
          key={String(selectedArtistId)}
          artistId={selectedArtistId}
          isAdmin={isAdmin}
          canCollect={canCollect}
          lidarrMode={lidarrMode}
          onBack={() => setSelectedArtistId(null)}
          onCollect={openCollectPicker}
          onChanged={() => void refresh()}
          onToggleArtistMonitored={toggleArtistMonitored}
          onToggleAlbumMonitored={toggleAlbumMonitored}
          onToggleTrackMonitored={toggleTrackMonitored}
          onToast={showToast}
        />
        <AddToCollectionModal picker={picker} />
      </>
    );
  }

  if (selectedCollectionId !== null) {
    return (
      <>
        {toastNode}
        <CollectionDetail
          collectionId={selectedCollectionId}
          fallback={collections.find((c) => c.id === selectedCollectionId)}
          isAdmin={isAdmin}
          onBack={() => setSelectedCollectionId(null)}
          onDelete={(id, name) => void handleDeleteCollection(id, name)}
          onChanged={refresh}
          onToast={showToast}
        />
      </>
    );
  }

  const showScanBanner = !lidarrMode && (isScanning || Boolean(scanStatus?.is_scanning));
  const pagedTab = activeTab !== 'collections';

  return (
    <div className="space-y-3 sm:space-y-6">
      {toastNode}

      {stats && <LibraryStatsBar stats={stats} />}

      {showScanBanner && <LibraryScanBanner scanStatus={scanStatus} onCancel={() => void cancelScan()} />}

      <div className="flex items-center justify-between gap-2 sm:gap-4">
        <TapeTransportBay className="flex min-w-0 items-center gap-1.5 overflow-x-auto">
          {visibleTabs.map((tab) => (
            <TapeDeckButton key={tab.id} size="sm" active={activeTab === tab.id} onClick={() => setTab(tab.id)} icon={tab.icon}>
              {tab.label}
            </TapeDeckButton>
          ))}
        </TapeTransportBay>

        {/* Scanning is a native-library action; Lidarr manages its own files. */}
        {isAdmin && !lidarrMode && (
          <div className="flex shrink-0 items-center gap-2">
            {isScanning ? (
              <TapeDeckButton
                size="sm"
                variant="danger"
                onClick={() => void cancelScan()}
                icon={<Loader2 className="h-3.5 w-3.5 animate-spin" />}
              >
                Cancel Scan
              </TapeDeckButton>
            ) : (
              <TapeDeckButton
                size="sm"
                variant="amber"
                onClick={() => void triggerScan(false)}
                icon={<RefreshCw className="h-3.5 w-3.5" />}
              >
                Scan Library
              </TapeDeckButton>
            )}
          </div>
        )}
      </div>

      {lidarrStatus && lidarrStatus.is_migrating && <LidarrMigrationBanner status={lidarrStatus} />}

      <div className="flex items-stretch gap-2">
        <SearchBar value={searchInput} onChange={setSearchInput} placeholder={`Filter ${activeTab}...`} className="flex-1 min-w-0" />
        {pagedTab && (
          <TapeDeckButton
            size="sm"
            className="shrink-0"
            active={monitoredOnly}
            aria-pressed={monitoredOnly}
            onClick={() => setMonitoredOnly((v) => !v)}
          >
            Monitored only
          </TapeDeckButton>
        )}
      </div>

      {error && !isLoading && (
        <div role="alert" className="p-4 bg-red-950/40 border border-red-800/50 rounded-[4px] text-xs text-red-300 font-mono">
          {error}
        </div>
      )}

      {activeTab === 'artists' && (
        <ArtistsPanel
          query={query}
          monitoredOnly={monitoredOnly}
          isAdmin={isAdmin}
          reloadToken={catalogVersion}
          onOpenArtist={setSelectedArtistId}
          onToggleMonitored={toggleArtistMonitored}
          onModeChange={setListMode}
          onToast={showToast}
        />
      )}

      {activeTab === 'albums' && (
        <AlbumsPanel
          query={query}
          monitoredOnly={monitoredOnly}
          isAdmin={isAdmin}
          canCollect={canCollect}
          reloadToken={catalogVersion}
          onOpenAlbum={setAlbumForModal}
          onCollectAlbum={openCollectPicker}
          onToggleMonitored={toggleAlbumMonitored}
          onModeChange={setListMode}
          onToast={showToast}
        />
      )}

      {activeTab === 'tracks' &&
        (lidarrMode ? (
          <div className="p-6 border border-dashed border-[#222] rounded-[4px] text-center text-xs font-mono text-neutral-400">
            Lidarr manages tracks per album &mdash; open an album to browse its tracks
          </div>
        ) : (
          <TracksPanel
            query={query}
            monitoredOnly={monitoredOnly}
            isAdmin={isAdmin}
            reloadToken={catalogVersion}
            onToggleMonitored={toggleTrackMonitored}
            onToast={showToast}
          />
        ))}

      {activeTab === 'collections' && lidarrMode && (
        <div className="p-6 border border-dashed border-[#222] rounded-[4px] text-center text-xs font-mono text-neutral-400">
          Collections are available when TrackSeerr manages the library
        </div>
      )}

      {activeTab === 'collections' &&
        !lidarrMode &&
        (isLoading ? (
          <div className="flex flex-col items-center justify-center py-16 gap-3">
            <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
            <span className="text-xs uppercase tracking-widest text-neutral-400 font-mono">Loading Collections...</span>
          </div>
        ) : (
          <CollectionsPanel
            collections={collections}
            isAdmin={isAdmin}
            onOpen={setSelectedCollectionId}
            onDelete={(id, name) => void handleDeleteCollection(id, name)}
            onCreated={handleCollectionCreated}
            onToast={showToast}
          />
        ))}

      <AlbumDetailModal
        album={albumForModal}
        isAdmin={isAdmin}
        canCollect={canCollect}
        lidarrMode={lidarrMode}
        onClose={() => setAlbumForModal(null)}
        onCollect={openCollectPicker}
        onGoToArtist={setSelectedArtistId}
        onToggleTrackMonitored={toggleTrackMonitored}
        onToast={showToast}
      />
      <AddToCollectionModal picker={picker} />
    </div>
  );
};
