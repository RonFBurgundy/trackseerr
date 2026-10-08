import React, { useCallback, useEffect, useState } from 'react';
import { Disc, Eye, FileText, FolderInput, Layers, Loader2, Music, RefreshCw, User } from 'lucide-react';
import type { UseLibraryReturn, LibraryTab } from '@/hooks/useLibrary';
import type { AppRoute, LibraryRoute, NavigateOptions } from '@/hooks/useAppRoute';
import { useLibraryDrilldown } from '@/hooks/useLibraryDrilldown';
import type { AlbumItem, ArtistDiscographyAlbum, AudioPreviewTrack, DiscoveryItem } from '@/types/models';
import { useAddToCollection } from '@/hooks/useAddToCollection';
import { useDebouncedValue } from '@/hooks/useDebouncedValue';
import { useLibraryManager } from '@/hooks/useLibraryManager';
import { useToast } from '@/hooks/useToast';
import type { ManualImportScope } from '@/types/manualImport';
import { deleteCollection } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';
import { PageFrame } from '@/components/layout';
import { SearchBar, TapeDeckButton, TabStrip, ToastBanner, CassetteLoader } from '@/components/ui';
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
  RenameModal,
  type RenameScope,
  TracksPanel,
} from '@/components/library';
import { ManualImportModal } from '@/components/manualImport';
import type { UseIssuesReturn } from '@/hooks/useIssues';

export interface LibraryViewProps {
  libraryHook: UseLibraryReturn;
  isAdmin?: boolean;
  /** Current library route: sub-page (artists/albums/tracks/collections) plus any artist/collection/album drill-down. */
  route: LibraryRoute;
  /** Any route: library pages plus the "View in Discover" jump from an artist. */
  onNavigate: (route: AppRoute, options?: NavigateOptions) => void;
  /** Step up to `parent`: history back when the previous entry is that parent, otherwise replace with it. */
  onNavigateUp: (parent: AppRoute) => void;
  /** Request flow shared with Discover (rest-of-discography requests on the artist page). */
  onRequestItem: (item: DiscoveryItem) => Promise<void>;
  onRequestDiscography: (artist: string, albums: ArtistDiscographyAlbum[]) => Promise<void>;
  /** Admin report keys on the album modal. */
  issuesHook: UseIssuesReturn;
  /** Preview playback and request state for the release modal on the artist page. */
  onPlayTrack: (track: AudioPreviewTrack) => void;
  currentPreviewTrackId?: string;
  isPreviewPlaying?: boolean;
  requestedIds: ReadonlySet<string>;
}

const SEARCH_DEBOUNCE_MS = 250;

const TABS: Array<{ id: LibraryTab; label: string; icon: React.ReactNode }> = [
  { id: 'artists', label: 'Artists', icon: <User className="h-3.5 w-3.5" /> },
  { id: 'albums', label: 'Albums', icon: <Disc className="h-3.5 w-3.5" /> },
  { id: 'tracks', label: 'Tracks', icon: <Music className="h-3.5 w-3.5" /> },
  { id: 'collections', label: 'Collections', icon: <Layers className="h-3.5 w-3.5" /> },
];

export const LibraryView: React.FC<LibraryViewProps> = ({
  libraryHook,
  isAdmin = false,
  route,
  onNavigate,
  onNavigateUp,
  onRequestItem,
  onRequestDiscography,
  issuesHook,
  onPlayTrack,
  currentPreviewTrackId,
  isPreviewPlaying = false,
  requestedIds,
}) => {
  const activeTab = route.sub;
  const {
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
    reloadCatalog,
  } = libraryHook;

  const { toast, showToast } = useToast();
  const manager = useLibraryManager(isAdmin);
  const drill = useLibraryDrilldown(route, onNavigate, onNavigateUp, showToast);
  const { artistId: selectedArtistId, collectionId: selectedCollectionId, closeDetail } = drill;
  const onSubChange = useCallback(
    (sub: LibraryTab, options?: NavigateOptions): void => onNavigate({ tab: 'library', sub }, options),
    [onNavigate]
  );

  const [toolbarSlot, setToolbarSlot] = useState<HTMLDivElement | null>(null);
  const [searchInput, setSearchInput] = useState<string>('');
  const query = useDebouncedValue(searchInput, SEARCH_DEBOUNCE_MS);
  const [monitoredOnly, setMonitoredOnly] = useState<boolean>(false);
  const [importScope, setImportScope] = useState<ManualImportScope | null>(null);
  const [renameScope, setRenameScope] = useState<RenameScope | null>(null);
  /** Data source reported by the paged artists/albums responses; used until the manager settings load. */
  const [listMode, setListMode] = useState<string | null>(null);

  const lidarrMode = (manager.state?.mode ?? listMode) === 'lidarr';
  // Collections reference native album rows, so adding albums to them is a native-mode action.
  const canCollect = !lidarrMode;

  // Collections search is still resolved by the collections endpoint; the paged lists take `query` directly.
  // The paged catalog loads through the hook's tab; keep it in step with the route.
  useEffect(() => {
    setTab(activeTab);
  }, [activeTab, setTab]);

  useEffect(() => {
    setSearch(query);
  }, [query, setSearch]);

  // Collections reference native album ids, so the tab only exists when TrackSeerr manages the library.
  const visibleTabs = lidarrMode ? TABS.filter((t) => t.id !== 'collections') : TABS;

  // A deep link to a tab this mode does not offer lands on Artists without leaving a Back trap.
  useEffect(() => {
    if (lidarrMode && activeTab === 'collections') onSubChange('artists', { replace: true });
  }, [lidarrMode, activeTab, onSubChange]);

  const picker = useAddToCollection(showToast, refresh);

  const handleDeleteCollection = useCallback(
    async (id: string, name: string): Promise<void> => {
      try {
        const ok = await deleteCollection(id);
        if (!ok) {
          showToast('Failed to delete collection', 'error');
          return;
        }
        if (selectedCollectionId === id) closeDetail();
        await refresh();
        showToast(`Collection "${name}" deleted`);
      } catch (err: unknown) {
        showToast(errorMessage(err, 'Error deleting collection'), 'error');
      }
    },
    [refresh, showToast, selectedCollectionId, closeDetail]
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

  const openAlbumImport = useCallback(
    (album: AlbumItem): void => setImportScope({ kind: 'album', albumId: String(album.id), title: album.title ?? '' }),
    []
  );

  const toastNode = toast ? <ToastBanner message={toast.message} tone={toast.tone} /> : null;

  // Modals are portaled and mounted once, whichever page is showing, so their triggers always find them.
  const overlays = (
    <>
      <AlbumDetailModal
        album={drill.album}
        isAdmin={isAdmin}
        issuesHook={issuesHook}
        canCollect={canCollect}
        lidarrMode={lidarrMode}
        onClose={drill.closeAlbum}
        onCollect={openCollectPicker}
        onGoToArtist={drill.openArtist}
        onImportFiles={openAlbumImport}
        onRenameFiles={(album) => setRenameScope({ albumId: String(album.id), albumTitle: album.title || undefined })}
        onToggleTrackMonitored={toggleTrackMonitored}
        onToast={showToast}
      />
      <AddToCollectionModal picker={picker} />
      <ManualImportModal
        scope={importScope}
        onClose={() => setImportScope(null)}
        onImported={() => {
          showToast('Files imported');
          void reloadCatalog();
        }}
      />
      <RenameModal
        scope={renameScope}
        onClose={() => setRenameScope(null)}
        onRenamed={() => {
          showToast('Files renamed');
          void reloadCatalog();
        }}
      />
    </>
  );

  if (selectedArtistId !== null) {
    return (
      <>
        <PageFrame nav={toastNode} scroll={false}>
          <ArtistDetail
            key={selectedArtistId}
            artistId={selectedArtistId}
            isAdmin={isAdmin}
            canCollect={canCollect}
            lidarrMode={lidarrMode}
            onBack={closeDetail}
            onCollect={openCollectPicker}
            onOpenAlbum={drill.openAlbum}
            onImportAlbum={openAlbumImport}
            onRenameFiles={() => setRenameScope({ artistId: String(selectedArtistId) })}
            onChanged={() => void refresh()}
            onToggleArtistMonitored={toggleArtistMonitored}
            onToggleAlbumMonitored={toggleAlbumMonitored}
            onToggleTrackMonitored={toggleTrackMonitored}
            onToast={showToast}
            onRequestItem={onRequestItem}
            onRequestDiscography={onRequestDiscography}
            onViewInDiscover={(discoveryId) => onNavigate({ tab: 'discover', artistId: discoveryId })}
            onPlayTrack={onPlayTrack}
            currentPreviewTrackId={currentPreviewTrackId}
            isPreviewPlaying={isPreviewPlaying}
            requestedIds={requestedIds}
            issuesHook={issuesHook}
          />
        </PageFrame>
        {overlays}
      </>
    );
  }

  if (selectedCollectionId !== null) {
    return (
      <>
        <PageFrame nav={toastNode} scroll={false}>
          <CollectionDetail
            collectionId={selectedCollectionId}
            fallback={collections.find((c) => c.id === selectedCollectionId)}
            isAdmin={isAdmin}
            onBack={closeDetail}
            onDelete={(id, name) => void handleDeleteCollection(id, name)}
            onChanged={refresh}
            onToast={showToast}
          />
        </PageFrame>
        {overlays}
      </>
    );
  }

  const showScanBanner = !lidarrMode && (isScanning || Boolean(scanStatus?.is_scanning));
  const pagedTab = activeTab !== 'collections';

  const statsFooter = <LibraryStatsBar stats={stats} showLegend={activeTab === 'artists'} />;

  return (
    <>
    <PageFrame
      inlineActions
      nav={
      <TabStrip aria-label="Library sections">
        {visibleTabs.map((tab) => (
          <TapeDeckButton key={tab.id} size="sm" active={activeTab === tab.id} onClick={() => onSubChange(tab.id)} icon={tab.icon}>
            {tab.label}
          </TapeDeckButton>
        ))}
      </TabStrip>
      }
      actions={
        <div className="flex min-w-0 items-stretch gap-1.5">
          <SearchBar
            id="library-search"
            name="library-filter"
            ariaLabel={`Filter ${activeTab}`}
            value={searchInput}
            onChange={setSearchInput}
            placeholder={`Filter ${activeTab}...`}
            className="flex-1 min-w-0"
          />
          {pagedTab && (
            <TapeDeckButton
              size="sm"
              className="shrink-0"
              active={monitoredOnly}
              aria-pressed={monitoredOnly}
              aria-label="Monitored only"
              title="Monitored only"
              icon={<Eye className="h-3.5 w-3.5" />}
              collapseLabel="xl"
              onClick={() => setMonitoredOnly((v) => !v)}
            >
              Monitored
            </TapeDeckButton>
          )}
          {/* Select and sort controls from the active panel are portaled here. */}
          <div ref={setToolbarSlot} className="contents" />
          {isAdmin && !lidarrMode && (
            <TapeDeckButton
              size="sm"
              className="shrink-0"
              onClick={() => setImportScope({ kind: 'folder' })}
              icon={<FolderInput className="h-3.5 w-3.5" />}
              collapseLabel="xl"
              title="Manual import"
            >
              Manual import&hellip;
            </TapeDeckButton>
          )}
          {isAdmin && !lidarrMode && (
            <TapeDeckButton
              size="sm"
              className="shrink-0"
              onClick={() => setRenameScope({})}
              icon={<FileText className="h-3.5 w-3.5" />}
              collapseLabel="xl"
              title="Rename files"
            >
              Rename files&hellip;
            </TapeDeckButton>
          )}
          {/* Scanning is a native-library action; Lidarr manages its own files. */}
          {isAdmin && !lidarrMode && (
            isScanning ? (
              <TapeDeckButton
                size="sm"
                variant="danger"
                className="shrink-0"
                onClick={() => void cancelScan()}
                icon={<Loader2 className="h-3.5 w-3.5 animate-spin" />}
                collapseLabel="xl"
                title="Cancel scan"
              >
                Cancel Scan
              </TapeDeckButton>
            ) : (
              <TapeDeckButton
                size="sm"
                variant="amber"
                className="shrink-0"
                onClick={() => void triggerScan(false)}
                icon={<RefreshCw className="h-3.5 w-3.5" />}
                collapseLabel="xl"
                title="Scan library"
              >
                Scan Library
              </TapeDeckButton>
            )
          )}
        </div>
      }
    >
      {/* Status banners are one line each and scroll away with the content. */}
      <div className="space-y-2 pb-2 empty:hidden">
        {toastNode}
        {showScanBanner && <LibraryScanBanner scanStatus={scanStatus} onCancel={() => void cancelScan()} />}
        {lidarrStatus && lidarrStatus.is_migrating && <LidarrMigrationBanner status={lidarrStatus} />}
        {error && !isLoading && (
          <div role="alert" title={error} className="px-3 py-2 bg-red-950/40 border border-red-800/50 rounded-[4px] text-xs text-red-300 font-mono truncate">
            {error}
          </div>
        )}
      </div>
      {activeTab === 'artists' && (
        <ArtistsPanel
          query={query}
          monitoredOnly={monitoredOnly}
          isAdmin={isAdmin}
          reloadToken={catalogVersion}
          onOpenArtist={drill.openArtist}
          toolbarSlot={toolbarSlot}
          footer={statsFooter}
          onModeChange={setListMode}
          onToast={showToast}
        />
      )}

      {activeTab === 'albums' && (
        <AlbumsPanel
          query={query}
          monitoredOnly={monitoredOnly}
          isAdmin={isAdmin}
          reloadToken={catalogVersion}
          onOpenAlbum={drill.openAlbum}
          toolbarSlot={toolbarSlot}
          footer={statsFooter}
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
            toolbarSlot={toolbarSlot}
            footer={statsFooter}
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
          <div className="py-16">
            <CassetteLoader size="md" />
          </div>
        ) : (
          <CollectionsPanel
            collections={collections}
            isAdmin={isAdmin}
            onOpen={drill.openCollection}
            onDelete={(id, name) => void handleDeleteCollection(id, name)}
            onCreated={handleCollectionCreated}
            onToast={showToast}
          />
        ))}
    </PageFrame>
    {overlays}
    </>
  );
};
