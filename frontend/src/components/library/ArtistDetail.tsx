import React, { useCallback, useId, useMemo, useState } from 'react';
import {
  ArrowLeft,
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
import type { AlbumItem } from '@/types/models';
import { MONITOR_OPTIONS } from '@/types/monitoring';
import type { ReleaseProfilePreview } from '@/types/releaseProfiles';
import { useArtistDetail, type MonitorPreset } from '@/hooks/useArtistDetail';
import { useLidarrSearch } from '@/hooks/useLidarrSearch';
import { useReleaseProfileDryRun, useReleaseProfilePreview, useReleaseProfiles } from '@/hooks/useReleaseProfiles';
import { useBulkSelection } from '@/hooks/useBulkSelection';
import { useAlbumBulkEdit } from '@/hooks/useAlbumBulkEdit';
import { errorMessage } from '@/services/apiClient';
import { ConfirmDialog, MachinedCard, TactileSwitch, TapeDeckButton, TabStrip } from '@/components/ui';
import { ArtistAlbumCard } from './ArtistAlbumCard';
import { AlbumBulkBar } from './AlbumBulkBar';

type DiscographyTab = 'studio' | 'singles_eps' | 'live' | 'compilations';

export interface ArtistDetailProps {
  artistId: number | string;
  isAdmin: boolean;
  canCollect: boolean;
  /** Lidarr manages the library: per-track monitoring is unavailable and Search buttons appear. */
  lidarrMode: boolean;
  onBack: () => void;
  onCollect: (album: AlbumItem) => void;
  /** Called after a change that the paged lists should pick up. */
  onChanged: () => void;
  onToggleArtistMonitored: (artistId: number | string, monitored: boolean) => Promise<void>;
  onToggleAlbumMonitored: (albumId: number | string, monitored: boolean) => Promise<void>;
  onToggleTrackMonitored: (trackId: number | string, monitored: boolean) => Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

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
  onChanged,
  onToggleArtistMonitored,
  onToggleAlbumMonitored,
  onToggleTrackMonitored,
  onToast,
}) => {
  const detail = useArtistDetail(artistId, onToast, onChanged);
  const { artist, loading, refreshing, patchAlbumMonitored, patchArtistMonitored } = detail;
  const [tab, setTab] = useState<DiscographyTab>('studio');
  const categorized = useMemo(() => categorize(artist?.albums ?? []), [artist]);
  const [hideOutside, setHideOutside] = useState<boolean>(false);
  const releaseProfiles = useReleaseProfiles(isAdmin && !lidarrMode, onToast);
  const releaseProfileId = artist?.release_profile_id ?? null;
  const dryRun = useReleaseProfileDryRun(artistId, onToast);
  const [pendingProfile, setPendingProfile] = useState<{ id: number | null; preview: ReleaseProfilePreview } | null>(null);
  const [profileBusy, setProfileBusy] = useState<boolean>(false);

  const requestProfileChange = async (id: number | null): Promise<void> => {
    if (profileBusy) return;
    setProfileBusy(true);
    try {
      const preview = await dryRun(id);
      if (!preview) return;
      const w = preview.would_change;
      if (w.albums_to_monitor + w.albums_to_unmonitor + w.tracks_to_monitor + w.tracks_to_unmonitor === 0) {
        await detail.applyReleaseProfile(id, true, w);
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
      await detail.applyReleaseProfile(id, applyToExisting, preview.would_change);
    } finally {
      setPendingProfile(null);
      setProfileBusy(false);
    }
  };

  const pendingName =
    pendingProfile?.id == null
      ? 'no release profile'
      : (releaseProfiles.profiles.find((p) => p.id === pendingProfile.id)?.name ?? 'the profile');
  const pendingParts: string[] = [];
  if (pendingProfile) {
    const w = pendingProfile.preview.would_change;
    const plural = (n: number, one: string): string => `${n} ${one}${n === 1 ? '' : 's'}`;
    if (w.albums_to_monitor > 0 || w.tracks_to_monitor > 0)
      pendingParts.push(`monitor ${plural(w.albums_to_monitor, 'album')} (${plural(w.tracks_to_monitor, 'track')})`);
    if (w.albums_to_unmonitor > 0 || w.tracks_to_unmonitor > 0)
      pendingParts.push(`unmonitor ${plural(w.albums_to_unmonitor, 'album')} (${plural(w.tracks_to_unmonitor, 'track')})`);
  }

  const profilePreview = useReleaseProfilePreview(artistId, releaseProfileId, artist?.albums?.length ?? 0);
  const hasOutside = releaseProfileId !== null && (artist?.albums ?? []).some((a) => a.in_profile === false);
  const albums = useMemo(
    () => (hideOutside && hasOutside ? categorized[tab].filter((a) => a.in_profile !== false) : categorized[tab]),
    [categorized, tab, hideOutside, hasOutside]
  );

  const canBulkEdit = isAdmin && !lidarrMode;
  const selection = useBulkSelection();
  const { exit: exitSelection } = selection;
  const bulk = useAlbumBulkEdit(onToast);

  const handleBulkApply = async (monitored: boolean): Promise<void> => {
    const ids = Array.from(selection.selected);
    if (await bulk.apply(ids, monitored)) {
      const picked = new Set(ids);
      for (const a of artist?.albums ?? []) {
        if (picked.has(a.id)) patchAlbumMonitored(a.id, monitored);
      }
      exitSelection();
      onChanged();
    }
  };

  const genreList = Array.isArray(artist?.genres)
    ? artist.genres
    : typeof artist?.genres === 'string'
      ? artist.genres.split(',').map((g) => g.trim()).filter(Boolean)
      : [];

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
  const releaseProfileSelectId = useId();
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
    <div className="space-y-3 sm:space-y-4">
      <div className="flex items-center justify-between">
        <TapeDeckButton size="sm" onClick={onBack} icon={<ArrowLeft className="h-4 w-4" />}>
          Back to Artists
        </TapeDeckButton>
        {isAdmin && (
          <div className="flex items-center gap-2">
            {lidarrMode && (
              <TapeDeckButton
                size="sm"
                disabled={lidarrSearch.busyKey === `artist:${artistId}`}
                onClick={() => void lidarrSearch.searchArtist(artistId)}
                icon={
                  lidarrSearch.busyKey === `artist:${artistId}` ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Search className="h-3.5 w-3.5" />
                  )
                }
                title="Ask Lidarr to search for this artist's monitored missing albums"
              >
                Search
              </TapeDeckButton>
            )}
            <TapeDeckButton
              size="sm"
              variant="amber"
              disabled={refreshing}
              onClick={() => void detail.refreshDiscography()}
              icon={refreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            >
              Refresh Discography
            </TapeDeckButton>
          </div>
        )}
      </div>

      <MachinedCard className="relative overflow-hidden">
        {artist?.banner_url && (
          <div className="absolute inset-0 z-0 pointer-events-none">
            <img src={artist.banner_url} alt="" className="w-full h-full object-cover opacity-20 filter blur-xs" />
            <div className="absolute inset-0 bg-gradient-to-t from-[#141414] via-[#141414]/85 to-transparent" />
          </div>
        )}
        <div className="relative z-10 grid grid-cols-[6.5rem_minmax(0,1fr)] sm:grid-cols-[auto_minmax(0,1fr)_auto] gap-x-3 gap-y-2 p-3 sm:gap-x-4 sm:p-4">
          <div className="col-start-1 row-start-1 sm:col-start-1 sm:row-span-3 relative h-[6.5rem] w-[6.5rem] sm:h-auto sm:w-36 rounded-[4px] bg-[#1a1a1a] border-2 border-[#e5a00d]/70 overflow-hidden flex items-center justify-center shadow-xl">
            {artist?.image_url ? (
              <img
                src={artist.image_url}
                alt={artist.name}
                className="absolute inset-0 w-full h-full object-cover"
                loading="lazy"
                decoding="async"
              />
            ) : (
              <User className="h-12 w-12 text-neutral-600" />
            )}
          </div>

          <div className="col-start-2 row-start-1 sm:col-start-2 min-w-0 flex flex-col justify-center text-center sm:text-left">
            <span className="hidden sm:block text-[10px] font-mono uppercase tracking-widest text-[#e5a00d]">
              Artist Catalog
            </span>
            <h2 className="text-lg sm:text-3xl font-black text-white font-mono tracking-tight leading-tight line-clamp-2 sm:line-clamp-1 break-words">
              {artist?.name}
            </h2>
          </div>

          <div className="col-span-2 row-start-3 sm:contents flex items-center gap-2">
            {artist?.mbid && (
              <a
                href={`https://musicbrainz.org/artist/${artist.mbid}`}
                target="_blank"
                rel="noopener noreferrer"
                className="sm:col-start-3 sm:row-start-2 sm:self-center inline-flex min-h-[44px] sm:min-h-0 shrink-0 px-2 sm:py-0.5 items-center rounded-[2px] text-[10px] font-mono font-bold bg-[#ba478f]/20 text-[#e599cf] border border-[#ba478f]/40 gap-1 hover:bg-[#ba478f]/30 transition-colors"
                title={`MusicBrainz Artist: ${artist.mbid}`}
              >
                <span>MusicBrainz</span>
                <ExternalLink className="h-3 w-3" />
              </a>
            )}
            {isAdmin && artist && (
              <div className="flex flex-1 min-w-0 items-center justify-end gap-2 sm:col-start-3 sm:row-start-1 sm:flex-none">
                <label htmlFor={presetId} className="sr-only">
                  Apply monitor preset
                </label>
                <select
                  id={presetId}
                  name="monitor_preset"
                  className="bg-[#141414] border border-[#2a2a2a] text-xs font-mono text-neutral-300 rounded-[3px] px-2 min-h-[44px] sm:min-h-[36px] focus:border-[#e5a00d] focus:outline-none flex-1 min-w-0 sm:flex-none sm:w-48"
                  value=""
                  onChange={(e) => {
                    const picked = presetOptions.find((o) => o.value === e.target.value);
                    if (picked) preset(picked.value);
                  }}
                >
                  <option value="" disabled>
                    Monitor preset...
                  </option>
                  {presetOptions.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
                {!lidarrMode && (
                  <>
                    <label htmlFor={releaseProfileSelectId} className="sr-only">
                      Release profile
                    </label>
                    <select
                      id={releaseProfileSelectId}
                      name="release_profile"
                      className="bg-[#141414] border border-[#2a2a2a] text-xs font-mono text-neutral-300 rounded-[3px] px-2 min-h-[44px] sm:min-h-[36px] focus:border-[#e5a00d] focus:outline-none flex-1 min-w-0 sm:flex-none sm:w-44"
                      value={releaseProfileId === null ? '' : String(releaseProfileId)}
                      disabled={profileBusy}
                      onChange={(e) => void requestProfileChange(e.target.value === '' ? null : Number(e.target.value))}
                    >
                      <option value="">No release profile</option>
                      {releaseProfiles.profiles.map((p) => (
                        <option key={p.id} value={String(p.id)}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                  </>
                )}
                <TactileSwitch
                  checked={artist.monitored}
                  onChange={(val) => void toggleArtist(val)}
                  label={artist.monitored ? 'Monitored' : 'Unmonitored'}
                  className="shrink-0 max-sm:[&>span]:sr-only"
                />
              </div>
            )}
          </div>

          <div
            className="col-span-2 row-start-2 sm:col-span-1 sm:col-start-2 flex flex-nowrap items-center gap-2 overflow-x-auto snap-x snap-proximity [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
            aria-label="Genres"
          >
            {artist?.country && (
              <span className="snap-start shrink-0 px-2 py-0.5 rounded-[2px] text-[10px] font-mono font-bold bg-neutral-800 text-neutral-200 border border-neutral-700 flex items-center gap-1">
                <Globe className="h-3 w-3 text-[#e5a00d]" />
                {artist.country}
              </span>
            )}
            {genreList.map((g) => (
              <span
                key={g}
                className="snap-start shrink-0 whitespace-nowrap px-2 py-0.5 rounded-[2px] text-[10px] font-mono bg-[#e5a00d]/10 text-[#e5a00d] border border-[#e5a00d]/30"
              >
                {g}
              </span>
            ))}
          </div>

          {artist?.bio && (
            <p className="hidden sm:block sm:col-start-2 sm:col-span-2 text-xs text-neutral-300 font-mono line-clamp-1 bg-black/40 px-2.5 py-1.5 rounded-[3px] border border-white/5">
              {artist.bio}
            </p>
          )}
        </div>

        <div className="relative z-10 flex items-center justify-center sm:justify-start gap-3 border-t border-[#1f1f1f] bg-black/30 px-3 sm:px-4 py-1 text-[11px] font-mono text-neutral-400">
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

        <div className="relative z-10 flex items-center gap-2 border-t border-[#1f1f1f] p-1.5">
          <TabStrip className="min-w-0 flex-1">
            {tabs.map((t) => (
              <TapeDeckButton key={t.id} size="sm" active={tab === t.id} onClick={() => setTab(t.id)} icon={t.icon}>
                <span className="hidden sm:inline">{t.full}</span>
                <span className="sm:hidden">{t.short}</span> ({categorized[t.id].length})
              </TapeDeckButton>
            ))}
          </TabStrip>
          {hasOutside && (
            <TactileSwitch
              checked={hideOutside}
              onChange={setHideOutside}
              label="Hide outside profile"
              className="shrink-0 max-sm:[&>span]:sr-only"
              title="Hide releases that are outside the release profile (they are never hidden by default)"
            />
          )}
          {canBulkEdit && !loading && !selection.active && (
            <TapeDeckButton
              size="sm"
              className="shrink-0"
              icon={<CheckSquare className="h-3.5 w-3.5" />}
              onClick={selection.enter}
              aria-label="Select albums"
            >
              <span className="hidden sm:inline">Select albums</span>
              <span className="sm:hidden">Select</span>
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
              onApply={(m) => void handleBulkApply(m)}
            />
          </div>
        )}
      </MachinedCard>

      {loading ? (
        <div className="flex flex-col items-center justify-center py-20 gap-3">
          <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
          <span className="text-xs uppercase tracking-widest text-neutral-400 font-mono">Loading Artist Discography...</span>
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
              onToggleAlbumMonitored={(id, cur) => void toggleAlbum(id, cur)}
              onToggleTrackMonitored={onToggleTrackMonitored}
              onToast={onToast}
              selected={
                canBulkEdit && selection.active
                  ? { checked: selection.isSelected(album.id), onToggle: () => selection.toggle(album.id) }
                  : undefined
              }
            />
          ))}
          {albums.length === 0 && (
            <div className="text-center py-12 text-neutral-500 font-mono text-sm">
              No releases categorized under this tab.
            </div>
          )}
        </div>
      )}
      <ConfirmDialog
        isOpen={pendingProfile !== null}
        title="Apply release profile?"
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
  );
};
