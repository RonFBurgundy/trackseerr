import React, { useCallback, useMemo, useState } from 'react';
import {
  ArrowLeft,
  Disc,
  ExternalLink,
  Globe,
  HardDrive,
  Loader2,
  Music,
  Radio,
  RefreshCw,
  Search,
  Sliders,
  User,
} from 'lucide-react';
import type { AlbumItem } from '@/types/models';
import { useArtistDetail, type MonitorPreset } from '@/hooks/useArtistDetail';
import { useLidarrSearch } from '@/hooks/useLidarrSearch';
import { errorMessage } from '@/services/apiClient';
import { MachinedCard, TactileSwitch, TapeDeckButton, TabStrip } from '@/components/ui';
import { ArtistAlbumCard } from './ArtistAlbumCard';

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
  const albums = categorized[tab];

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
  const preset = (p: MonitorPreset): void => void detail.applyPreset(p);

  const tabs: Array<{ id: DiscographyTab; full: string; short: string; icon: React.ReactNode }> = [
    { id: 'studio', full: 'Studio Albums', short: 'Studio', icon: <Disc className="h-3.5 w-3.5" /> },
    { id: 'singles_eps', full: 'EPs & Singles', short: 'Singles', icon: <Music className="h-3.5 w-3.5" /> },
    { id: 'live', full: 'Live Recordings', short: 'Live', icon: <Radio className="h-3.5 w-3.5" /> },
    { id: 'compilations', full: 'Compilations & Box Sets', short: 'Compilations', icon: <HardDrive className="h-3.5 w-3.5" /> },
  ];

  return (
    <div className="space-y-6">
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

      <MachinedCard className="p-6 relative overflow-hidden">
        {artist?.banner_url && (
          <div className="absolute inset-0 z-0 pointer-events-none">
            <img src={artist.banner_url} alt="" className="w-full h-full object-cover opacity-20 filter blur-xs" />
            <div className="absolute inset-0 bg-gradient-to-t from-[#141414] via-[#141414]/85 to-transparent" />
          </div>
        )}
        <div className="relative z-10 flex flex-col sm:flex-row items-center sm:items-start gap-6">
          <div className="h-32 w-32 rounded-[4px] bg-[#1a1a1a] border-2 border-[#e5a00d]/70 overflow-hidden flex-shrink-0 flex items-center justify-center shadow-xl">
            {artist?.image_url ? (
              <img src={artist.image_url} alt={artist.name} className="w-full h-full object-cover" loading="lazy" />
            ) : (
              <User className="h-16 w-16 text-neutral-600" />
            )}
          </div>

          <div className="flex-1 text-center sm:text-left space-y-3 min-w-0">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div className="min-w-0">
                <span className="text-[10px] font-mono uppercase tracking-widest text-[#e5a00d]">Artist Catalog</span>
                <h2 className="text-2xl sm:text-3xl font-black text-white font-mono tracking-tight mt-0.5 truncate">
                  {artist?.name}
                </h2>
              </div>
              {isAdmin && artist && (
                <div className="flex items-center justify-center sm:justify-end gap-2 flex-shrink-0">
                  <TactileSwitch
                    checked={artist.monitored}
                    onChange={(val) => void toggleArtist(val)}
                    label={artist.monitored ? 'Monitored' : 'Unmonitored'}
                  />
                </div>
              )}
            </div>

            <div className="flex flex-wrap items-center justify-center sm:justify-start gap-2 pt-0.5">
              {artist?.country && (
                <span className="px-2 py-0.5 rounded-[2px] text-[10px] font-mono font-bold bg-neutral-800 text-neutral-200 border border-neutral-700 flex items-center gap-1">
                  <Globe className="h-3 w-3 text-[#e5a00d]" />
                  {artist.country}
                </span>
              )}
              {genreList.map((g) => (
                <span
                  key={g}
                  className="px-2 py-0.5 rounded-[2px] text-[10px] font-mono bg-[#e5a00d]/10 text-[#e5a00d] border border-[#e5a00d]/30"
                >
                  {g}
                </span>
              ))}
              {artist?.mbid && (
                <a
                  href={`https://musicbrainz.org/artist/${artist.mbid}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="px-2 py-0.5 rounded-[2px] text-[10px] font-mono font-bold bg-[#ba478f]/20 text-[#e599cf] border border-[#ba478f]/40 flex items-center gap-1 hover:bg-[#ba478f]/30 transition-colors"
                  title={`MusicBrainz Artist: ${artist.mbid}`}
                >
                  <span>MusicBrainz</span>
                  <ExternalLink className="h-3 w-3" />
                </a>
              )}
            </div>

            {artist?.bio && (
              <p className="text-xs text-neutral-300 font-mono line-clamp-2 bg-black/40 p-2.5 rounded-[3px] border border-white/5 text-left">
                {artist.bio}
              </p>
            )}

            <div className="flex flex-wrap items-center justify-center sm:justify-start gap-4 text-xs font-mono text-neutral-400">
              <span>{artist?.albums?.length || artist?.album_count || 0} Releases</span>
              <span>&bull;</span>
              <span>{artist?.track_count || 0} Tracks in Library</span>
            </div>

            {isAdmin && (
              <div className="pt-2 border-t border-[#1f1f1f]">
                <div className="flex sm:hidden items-center gap-2">
                  <span className="text-[11px] font-mono text-neutral-400 flex items-center gap-1.5 shrink-0">
                    <Sliders className="h-3 w-3 text-[#e5a00d]" /> Monitor:
                  </span>
                  <select
                    className="bg-[#141414] border border-[#2a2a2a] text-xs font-mono text-neutral-300 rounded-[3px] px-2 min-h-[44px] focus:border-[#e5a00d] focus:outline-none flex-1 min-w-0"
                    value=""
                    onChange={(e) => {
                      if (e.target.value) preset(e.target.value as MonitorPreset);
                    }}
                  >
                    <option value="" disabled>
                      Apply Monitor Preset...
                    </option>
                    <option value="all">Monitor All</option>
                    <option value="albums">Studio Albums Only</option>
                    <option value="singles_eps">Singles & EPs Only</option>
                    <option value="none">Unmonitor All</option>
                  </select>
                </div>
                <div className="hidden sm:flex flex-wrap items-center gap-2">
                  <span className="text-[11px] font-mono text-neutral-400 flex items-center gap-1.5 mr-1">
                    <Sliders className="h-3 w-3 text-[#e5a00d]" /> Monitor Presets:
                  </span>
                  <TapeDeckButton size="sm" onClick={() => preset('all')} className="text-xs py-1">
                    Monitor All
                  </TapeDeckButton>
                  <TapeDeckButton size="sm" onClick={() => preset('albums')} className="text-xs py-1">
                    Studio Albums Only
                  </TapeDeckButton>
                  <TapeDeckButton size="sm" onClick={() => preset('singles_eps')} className="text-xs py-1">
                    Singles &amp; EPs Only
                  </TapeDeckButton>
                  <TapeDeckButton size="sm" onClick={() => preset('none')} className="text-xs py-1 text-neutral-400">
                    Unmonitor All
                  </TapeDeckButton>
                </div>
              </div>
            )}
          </div>
        </div>
      </MachinedCard>

      <TabStrip>
        {tabs.map((t) => (
          <TapeDeckButton key={t.id} size="sm" active={tab === t.id} onClick={() => setTab(t.id)} icon={t.icon}>
            <span className="hidden sm:inline">{t.full}</span>
            <span className="sm:hidden">{t.short}</span> ({categorized[t.id].length})
          </TapeDeckButton>
        ))}
      </TabStrip>

      {loading ? (
        <div className="flex flex-col items-center justify-center py-20 gap-3">
          <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
          <span className="text-xs uppercase tracking-widest text-neutral-400 font-mono">Loading Artist Discography...</span>
        </div>
      ) : (
        <div className="space-y-4">
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
            />
          ))}
          {albums.length === 0 && (
            <div className="text-center py-12 text-neutral-500 font-mono text-sm">
              No releases categorized under this tab.
            </div>
          )}
        </div>
      )}
    </div>
  );
};
