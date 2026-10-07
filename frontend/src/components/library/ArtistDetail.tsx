import React, { useCallback, useEffect, useId, useMemo, useState } from 'react';
import {
  CheckSquare,
  Disc,
  ExternalLink,
  Globe,
  HardDrive,
  Loader2,
  Music,
  Radio,
  RefreshCw,
  Search,
  User,
} from 'lucide-react';
import type { AlbumItem, ArtistDiscographyAlbum, AudioPreviewTrack, DiscoveryItem } from '@/types/models';
import type { UseIssuesReturn } from '@/hooks/useIssues';
import { MONITOR_OPTIONS } from '@/types/monitoring';
import type { MetadataProfilePreview } from '@/types/metadataProfiles';
import { useArtistDetail, type MonitorPreset } from '@/hooks/useArtistDetail';
import { useLidarrSearch } from '@/hooks/useLidarrSearch';
import { useMetadataProfileDryRun, useMetadataProfilePreview, useMetadataProfiles } from '@/hooks/useMetadataProfiles';
import { useDiscographyFilter } from '@/hooks/useDiscographyFilter';
import { useBulkSelection } from '@/hooks/useBulkSelection';
import { useAlbumBulkEdit } from '@/hooks/useAlbumBulkEdit';
import { errorMessage } from '@/services/apiClient';
import { ConfirmDialog, MachinedCard, TactileSwitch, TapeDeckButton, TabStrip, CassetteLoader } from '@/components/ui';
import { DetailHeaderBar, PageFrame } from '@/components/layout';

import { ArtistAlbumCard } from './ArtistAlbumCard';
import { ItemOriginCaption } from './ItemOriginCaption';
import { AlbumBulkBar } from './AlbumBulkBar';
import { DiscographyFilter } from './DiscographyFilter';
import { ArtistRestOfDiscography } from './ArtistRestOfDiscography';
import { ArtistTagsRow } from './ArtistTagsRow';
import { genreNames } from './genres';

type DiscographyTab = 'studio' | 'singles_eps' | 'live' | 'compilations';

export interface ArtistDetailProps {
  artistId: number | string;
  isAdmin: boolean;
  canCollect: boolean;
  /** Lidarr manages the library: per-track monitoring is unavailable and Search buttons appear. */
  lidarrMode: boolean;
  onBack: () => void;
  onCollect: (album: AlbumItem) => void;
  /** Opens the album detail modal (route-driven). */
  onOpenAlbum: (album: AlbumItem) => void;
  /** Opens Manual Import scoped to an album (native mode, admin). */
  onImportAlbum: (album: AlbumItem) => void;
  /** Called after a change that the paged lists should pick up. */
  onChanged: () => void;
  onToggleArtistMonitored: (artistId: number | string, monitored: boolean) => Promise<void>;
  onToggleAlbumMonitored: (albumId: number | string, monitored: boolean) => Promise<void>;
  onToggleTrackMonitored: (trackId: number | string, monitored: boolean) => Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
  /** Request flow shared with Discover, for the admin-only rest-of-discography section. */
  onRequestItem: (item: DiscoveryItem) => Promise<void>;
  onRequestDiscography: (artist: string, albums: ArtistDiscographyAlbum[]) => Promise<void>;
  onViewInDiscover: (discoveryId: string) => void;
  /** Release detail modal wiring for the rest-of-discography section. */
  onPlayTrack: (track: AudioPreviewTrack) => void;
  currentPreviewTrackId?: string;
  isPreviewPlaying: boolean;
  requestedIds: ReadonlySet<string>;
  issuesHook: UseIssuesReturn;
}

/** 32px icon key: the page actions live inside the hero, so they stay small on every viewport. */
const ICON_KEY = '!min-h-8 !h-8 !w-8 !p-0 shrink-0';
const COMPACT_SELECT =
  "h-8 min-h-8 min-w-0 appearance-none rounded-[3px] border border-[#2a2a2a] bg-[#141414] bg-[url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%23a3a3a3' stroke-width='3'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E\")] bg-[length:10px] bg-[right_4px_center] bg-no-repeat py-0 pl-1.5 pr-4 text-xs font-mono text-neutral-300 focus:border-[#e5a00d] focus:outline-none disabled:opacity-50";

const SINGLE_TYPES = ['single', 'ep', 'singles', 'eps'];
const STUDIO_TYPES = ['album', 'studio'];

function categorize(albums: AlbumItem[]): Record<DiscographyTab, AlbumItem[]> {
  const type = (a: AlbumItem): string => (a.album_type ?? '').toLowerCase();
  return {
    studio: albums.filter((a) => !a.album_type || STUDIO_TYPES.includes(type(a))),
    singles_eps: albums.filter((a) => a.album_type && SINGLE_TYPES.includes(type(a))),
    live: albums.filter((a) => a.album_type && type(a) === 'live'),
    compilations: albums.filter(
      (a) => a.album_type && ![...STUDIO_TYPES, ...SINGLE_TYPES, 'live'].includes(type(a))
    ),
  };
}

export const ArtistDetail: React.FC<ArtistDetailProps> = ({
  artistId,
  isAdmin,
  canCollect,
  lidarrMode,
  onBack,
  onCollect,
  onOpenAlbum,
  onImportAlbum,
  onChanged,
  onToggleArtistMonitored,
  onToggleAlbumMonitored,
  onToggleTrackMonitored,
  onToast,
  onRequestItem,
  onRequestDiscography,
  onViewInDiscover,
  onPlayTrack,
  currentPreviewTrackId,
  isPreviewPlaying,
  requestedIds,
  issuesHook,
}) => {
  const detail = useArtistDetail(artistId, onToast, onChanged);
  const { artist, loading, refreshing, patchAlbumMonitored, patchArtistMonitored, patchArtistTags } = detail;
  const [tab, setTab] = useState<DiscographyTab>('studio');
  const filter = useDiscographyFilter(artistId);
  const { filterAlbums } = filter;
  const categorized = useMemo(() => categorize(filterAlbums(artist?.albums ?? [])), [artist, filterAlbums]);
  const [hideOutside, setHideOutside] = useState<boolean>(false);
  const metadataProfiles = useMetadataProfiles(isAdmin && !lidarrMode, onToast);
  const metadataProfileId = artist?.metadata_profile_id ?? null;
  const dryRun = useMetadataProfileDryRun(artistId, onToast);
  const [pendingProfile, setPendingProfile] = useState<{ id: number | null; preview: MetadataProfilePreview } | null>(null);
  const [profileBusy, setProfileBusy] = useState<boolean>(false);

  const requestProfileChange = async (id: number | null): Promise<void> => {
    if (profileBusy) return;
    setProfileBusy(true);
    try {
      const preview = await dryRun(id);
      if (!preview) return;
      const w = preview.would_change;
      if (w.albums_to_monitor + w.albums_to_unmonitor + w.tracks_to_monitor + w.tracks_to_unmonitor === 0) {
        await detail.applyMetadataProfile(id, true, w);
      } else {
        setPendingProfile({ id, preview });
      }
    } finally {
      setProfileBusy(false);
    }
  };

  const resolveProfileChange = async (applyToExisting: boolean): Promise<void> => {
    if (!pendingProfile) return;
    const { id, preview } = pendingProfile;
    setProfileBusy(true);
    try {
      await detail.applyMetadataProfile(id, applyToExisting, preview.would_change);
    } finally {
      setPendingProfile(null);
      setProfileBusy(false);
    }
  };

  const pendingName =
    pendingProfile?.id == null
      ? 'no metadata profile'
      : (metadataProfiles.profiles.find((p) => p.id === pendingProfile.id)?.name ?? 'the profile');
  const pendingParts: string[] = [];
  if (pendingProfile) {
    const w = pendingProfile.preview.would_change;
    const plural = (n: number, one: string): string => `${n} ${one}${n === 1 ? '' : 's'}`;
    if (w.albums_to_monitor > 0 || w.tracks_to_monitor > 0)
      pendingParts.push(`monitor ${plural(w.albums_to_monitor, 'album')} (${plural(w.tracks_to_monitor, 'track')})`);
    if (w.albums_to_unmonitor > 0 || w.tracks_to_unmonitor > 0)
      pendingParts.push(`unmonitor ${plural(w.albums_to_unmonitor, 'album')} (${plural(w.tracks_to_unmonitor, 'track')})`);
  }

  const profilePreview = useMetadataProfilePreview(artistId, metadataProfileId, artist?.albums?.length ?? 0);
  const hasOutside = metadataProfileId !== null && (artist?.albums ?? []).some((a) => a.in_profile === false);
  const albums = useMemo(
    () => (hideOutside && hasOutside ? categorized[tab].filter((a) => a.in_profile !== false) : categorized[tab]),
    [categorized, tab, hideOutside, hasOutside]
  );

  // A filter that empties the open tab jumps to the first tab that still has matches.
  useEffect(() => {
    if (!filter.active || filter.searching || categorized[tab].length > 0) return;
    const next = (['studio', 'singles_eps', 'live', 'compilations'] as const).find((id) => categorized[id].length > 0);
    if (next) setTab(next);
  }, [filter.active, filter.searching, categorized, tab]);

  const canBulkEdit = isAdmin && !lidarrMode;
  const selection = useBulkSelection();
  const { exit: exitSelection } = selection;
  const bulk = useAlbumBulkEdit(onToast);

  const handleBulkApply = async (monitored: boolean): Promise<boolean> => {
    const ids = Array.from(selection.selected);
    const ok = await bulk.apply(ids, monitored);
    if (ok) {
      const picked = new Set(ids);
      for (const a of artist?.albums ?? []) {
        if (picked.has(a.id)) patchAlbumMonitored(a.id, monitored);
      }
      onChanged();
    }
    return ok;
  };

  const genreList = genreNames(artist?.genres);

  const toggleAlbum = useCallback(
    async (albumId: number | string, current: boolean): Promise<void> => {
      try {
        await onToggleAlbumMonitored(albumId, !current);
        patchAlbumMonitored(albumId, !current);
        onChanged();
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to update album monitoring'), 'error');
      }
    },
    [onToggleAlbumMonitored, patchAlbumMonitored, onChanged, onToast]
  );

  const toggleArtist = async (val: boolean): Promise<void> => {
    try {
      await onToggleArtistMonitored(artistId, val);
      patchArtistMonitored(val);
      onChanged();
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to update artist monitoring'), 'error');
    }
  };

  const lidarrSearch = useLidarrSearch(onToast);
  const presetId = useId();
  const metadataProfileSelectId = useId();
  const preset = (p: MonitorPreset): void => void detail.applyPreset(p);
  // Lidarr's own preset set has no existing/future; those are native-library only.
  const presetOptions = MONITOR_OPTIONS.filter(
    (o) => !lidarrMode || (o.value !== 'existing' && o.value !== 'future')
  );

  const tabs: Array<{ id: DiscographyTab; full: string; short: string; icon: React.ReactNode }> = [
    { id: 'studio', full: 'Studio Albums', short: 'Studio', icon: <Disc className="h-3.5 w-3.5" /> },
    { id: 'singles_eps', full: 'EPs & Singles', short: 'Singles', icon: <Music className="h-3.5 w-3.5" /> },
    { id: 'live', full: 'Live Recordings', short: 'Live', icon: <Radio className="h-3.5 w-3.5" /> },
    { id: 'compilations', full: 'Compilations & Box Sets', short: 'Compilations', icon: <HardDrive className="h-3.5 w-3.5" /> },
  ];

  return (
    <PageFrame nav={<DetailHeaderBar parentLabel="Artists" title={artist?.name} onBack={onBack} />}>
    <div className="space-y-2 sm:space-y-4">
      <MachinedCard className="relative overflow-hidden">
        {artist?.banner_url && (
          <div className="absolute inset-0 z-0 pointer-events-none">
            <img src={artist.banner_url} alt="" className="w-full h-full object-cover opacity-20 filter blur-xs" />
            <div className="absolute inset-0 bg-gradient-to-t from-[#141414] via-[#141414]/85 to-transparent" />
          </div>
        )}
        <div className="relative z-10 grid grid-cols-[7rem_minmax(0,1fr)] sm:grid-cols-[9rem_minmax(0,1fr)] gap-x-2 sm:gap-x-4 p-2 sm:p-4">
          <div className="relative h-28 w-28 sm:h-36 sm:w-36 rounded-[4px] bg-[#1a1a1a] border-2 border-[#e5a00d]/70 overflow-hidden flex items-center justify-center shadow-xl">
            {artist?.image_url ? (
              <img
                src={artist.image_url}
                alt={artist.name}
                className="absolute inset-0 w-full h-full object-cover"
                loading="lazy"
                decoding="async"
              />
            ) : (
              <User className="h-10 w-10 text-neutral-600" />
            )}
          </div>

          <div className="min-w-0 flex flex-col justify-between gap-1.5 sm:gap-2">
            <div className="flex items-start gap-1.5 min-w-0">
              <div className="min-w-0 flex-1">
                <span className="hidden sm:block text-[10px] font-mono uppercase tracking-widest text-[#e5a00d]">
                  Artist Catalog
                </span>
                <h2 className="text-[17px] sm:text-3xl font-black text-white font-mono tracking-tight leading-tight line-clamp-2 sm:line-clamp-1 break-words text-left">
                  {artist?.name}
                </h2>
                {artist && <ItemOriginCaption entity="artist" entityId={artist.id} title={artist.name} />}
              </div>
              {isAdmin && (
                <div className="flex shrink-0 items-center gap-1.5">
                  {lidarrMode && (
                    <TapeDeckButton
                      size="sm"
                      className={ICON_KEY}
                      disabled={lidarrSearch.busyKey === `artist:${artistId}`}
                      onClick={() => void lidarrSearch.searchArtist(artistId)}
                      icon={
                        lidarrSearch.busyKey === `artist:${artistId}` ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <Search className="h-4 w-4" />
                        )
                      }
                      aria-label="Search for this artist"
                      title="Ask Lidarr to search for this artist's monitored missing albums"
                    />
                  )}
                  <TapeDeckButton
                    size="sm"
                    variant="amber"
                    className={ICON_KEY}
                    disabled={refreshing}
                    onClick={() => void detail.refreshDiscography()}
                    icon={refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                    aria-label="Refresh discography"
                    title="Refresh discography"
                  />
                </div>
              )}
            </div>

            <div
              className="flex flex-nowrap items-center gap-1.5 overflow-x-auto snap-x snap-proximity [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
              aria-label="Genres"
            >
              {artist?.country && (
                <span className="snap-start shrink-0 inline-flex h-[22px] items-center gap-1 px-1.5 rounded-[2px] text-[11px] font-mono font-bold bg-neutral-800 text-neutral-200 border border-neutral-700">
                  <Globe className="h-3 w-3 text-[#e5a00d]" />
                  {artist.country}
                </span>
              )}
              {genreList.map((g) => (
                <span
                  key={g}
                  className="snap-start shrink-0 whitespace-nowrap inline-flex h-[22px] items-center px-1.5 rounded-[2px] text-[11px] font-mono bg-[#e5a00d]/10 text-[#e5a00d] border border-[#e5a00d]/30"
                >
                  {g}
                </span>
              ))}
            </div>

            {isAdmin && !lidarrMode && artist && (
              <ArtistTagsRow
                artistId={artistId}
                artistName={artist.name}
                tagIds={artist.tags ?? []}
                onSaved={patchArtistTags}
                onToast={onToast}
              />
            )}

            <div className="flex items-center gap-1 sm:gap-2 min-w-0">
              {artist?.mbid && (
                <a
                  href={`https://musicbrainz.org/artist/${artist.mbid}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="relative inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[3px] bg-[#ba478f]/20 text-[#e599cf] border border-[#ba478f]/40 hover:bg-[#ba478f]/30 transition-colors after:absolute after:-inset-1 after:content-['']"
                  title={`MusicBrainz Artist: ${artist.mbid}`}
                  aria-label="Open on MusicBrainz"
                >
                  <ExternalLink className="h-4 w-4" />
                </a>
              )}
              {isAdmin && artist && (
                <>
                  <label htmlFor={presetId} className="sr-only">
                    Apply monitor preset
                  </label>
                  <select
                    id={presetId}
                    name="monitor_preset"
                    className={`${COMPACT_SELECT} flex-[4_1_0] sm:flex-none sm:w-48`}
                    value=""
                    onChange={(e) => {
                      const picked = presetOptions.find((o) => o.value === e.target.value);
                      if (picked) preset(picked.value);
                    }}
                  >
                    <option value="" disabled>
                      Monitor
                    </option>
                    {presetOptions.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                  {!lidarrMode && (
                    <>
                      <label htmlFor={metadataProfileSelectId} className="sr-only">
                        Metadata profile
                      </label>
                      <select
                        id={metadataProfileSelectId}
                        name="metadata_profile"
                        className={`${COMPACT_SELECT} flex-[5_1_0] sm:flex-none sm:w-44`}
                        value={metadataProfileId === null ? '' : String(metadataProfileId)}
                        disabled={profileBusy}
                        onChange={(e) => void requestProfileChange(e.target.value === '' ? null : Number(e.target.value))}
                      >
                        <option value="">No profile</option>
                        {metadataProfiles.profiles.map((p) => (
                          <option key={p.id} value={String(p.id)}>
                            {p.name}
                          </option>
                        ))}
                      </select>
                    </>
                  )}
                  <TactileSwitch
                    checked={artist.monitored === true}
                    onChange={(val) => void toggleArtist(val)}
                    label={artist.monitored ? 'Monitored' : 'Unmonitored'}
                    className="shrink-0 max-sm:-mx-1 max-sm:[&>span]:sr-only"
                  />
                </>
              )}
            </div>

            {artist?.bio && (
              <p className="hidden sm:block text-xs text-neutral-300 font-mono line-clamp-1 bg-black/40 px-2.5 py-1.5 rounded-[3px] border border-white/5">
                {artist.bio}
              </p>
            )}
          </div>
        </div>

        <div className="relative z-10 flex items-center justify-start gap-2 sm:gap-3 border-t border-[#1f1f1f] bg-black/30 px-2 sm:px-4 py-0.5 sm:py-1 text-[11px] leading-4 whitespace-nowrap overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden font-mono text-neutral-400">
          <span>{artist?.albums?.length || artist?.album_count || 0} Releases</span>
          <span aria-hidden="true">&bull;</span>
          <span>{artist?.track_count || 0} Tracks in Library</span>
          {profilePreview.preview && (
            <>
              <span aria-hidden="true">&bull;</span>
              <span title="Releases outside the profile are not auto-monitored but stay in the catalog">
                {profilePreview.preview.matching} of {profilePreview.preview.total} releases in profile
              </span>
            </>
          )}
        </div>

        <div className="relative z-10 flex items-center gap-1.5 border-t border-[#1f1f1f] p-1 sm:p-1.5">
          <TabStrip className="min-w-0 flex-1">
            {tabs.map((t) => (
              <TapeDeckButton key={t.id} size="sm" active={tab === t.id} onClick={() => setTab(t.id)} icon={t.icon}>
                <span className="hidden sm:inline">{t.full}</span>
                <span className="sm:hidden">{t.short}</span> ({categorized[t.id].length})
              </TapeDeckButton>
            ))}
          </TabStrip>
          <DiscographyFilter value={filter.text} onChange={filter.setText} onClear={filter.clear} searching={filter.searching} />
          {hasOutside && (
            <TactileSwitch
              checked={hideOutside}
              onChange={setHideOutside}
              label="Hide outside profile"
              className="shrink-0 max-sm:[&>span]:sr-only"
              title="Hide releases that are outside the metadata profile (they are never hidden by default)"
            />
          )}
          {canBulkEdit && !loading && !selection.active && (
            <TapeDeckButton
              size="sm"
              className="shrink-0"
              icon={<CheckSquare className="h-3.5 w-3.5" />}
              collapseLabel
              onClick={selection.enter}
              aria-label="Select albums"
              title="Select albums"
            >
              Select albums
            </TapeDeckButton>
          )}
        </div>
        {canBulkEdit && !loading && selection.active && (
          <div className="relative z-10 border-t border-[#1f1f1f] p-1.5">
            <AlbumBulkBar
              count={selection.selected.size}
              busy={bulk.busy}
              selectLabel="Select tab"
              onSelectAll={() => selection.selectKeys(albums.map((a) => a.id))}
              onClear={selection.clear}
              onDone={exitSelection}
              onApply={handleBulkApply}
            />
          </div>
        )}
      </MachinedCard>

      {loading ? (
        <div className="py-20">
          <CassetteLoader size="md" />
        </div>
      ) : (
        <div className="space-y-2 sm:space-y-4">
          {albums.map((album) => (
            <ArtistAlbumCard
              key={album.id}
              album={album}
              isAdmin={isAdmin}
              canCollect={canCollect}
              lidarrMode={lidarrMode}
              onCollect={onCollect}
              onOpenAlbum={onOpenAlbum}
              onImportAlbum={onImportAlbum}
              onToggleAlbumMonitored={(id, cur) => void toggleAlbum(id, cur)}
              onToggleTrackMonitored={onToggleTrackMonitored}
              onToast={onToast}
              trackFilter={filter.trackFilterFor(album.id)}
              selected={
                canBulkEdit && selection.active
                  ? { checked: selection.isSelected(album.id), onToggle: () => selection.toggle(album.id) }
                  : undefined
              }
            />
          ))}
          {albums.length === 0 && (
            <div className="text-center py-12 text-neutral-500 font-mono text-sm">
              {filter.active ? 'No releases match' : 'No releases categorized under this tab.'}
            </div>
          )}
        </div>
      )}
      {isAdmin && !loading && artist && (
        <ArtistRestOfDiscography
          libraryArtistId={String(artistId)}
          onRequestItem={onRequestItem}
          onRequestDiscography={onRequestDiscography}
          onViewInDiscover={onViewInDiscover}
          onPlayTrack={onPlayTrack}
          currentPreviewTrackId={currentPreviewTrackId}
          isPreviewPlaying={isPreviewPlaying}
          requestedIds={requestedIds}
          issuesHook={issuesHook}
        />
      )}
      <ConfirmDialog
        isOpen={pendingProfile !== null}
        title="Apply metadata profile?"
        confirmLabel="Apply to existing"
        onConfirm={() => void resolveProfileChange(true)}
        secondaryLabel="Future releases only"
        onSecondary={() => void resolveProfileChange(false)}
        onCancel={() => setPendingProfile(null)}
        busy={profileBusy}
      >
        <p>
          Applying {pendingName} to existing releases will {pendingParts.join(' and ')}. Manual monitoring choices for
          this artist will be replaced.
        </p>
      </ConfirmDialog>
    </div>
    </PageFrame>
  );
};
