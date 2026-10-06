import React, { useCallback, useMemo } from 'react';
import { Compass, Disc, Loader2, Plus } from 'lucide-react';
import { MachinedCard, StatusMessage, TapeDeckButton } from '@/components/ui';
import { ProfileRequestButton, ProfileStatusBadge, profileAlbumToDiscography, profileAlbumToItem, releaseYear } from '@/components/discovery';
import { useArtistProfile } from '@/hooks/useArtistProfile';
import { useProfileRequests } from '@/hooks/useProfileRequests';
import type { ArtistDiscographyAlbum, ArtistProfileAlbum, DiscoveryItem } from '@/types/models';

export interface ArtistRestOfDiscographyProps {
  /** Library artist id of the page this renders on (admin-only endpoint). */
  libraryArtistId: string;
  onRequestItem: (item: DiscoveryItem) => Promise<void>;
  onRequestDiscography: (artist: string, albums: ArtistDiscographyAlbum[]) => Promise<void>;
  onViewInDiscover: (discoveryId: string) => void;
}

/** What we do not own yet: not in the library, partially owned releases stay on the artist page above. */
const NOT_OWNED: ReadonlySet<string> = new Set(['missing', 'none', 'rejected', 'requested', 'pending', 'processing']);
const ACTIONABLE: ReadonlySet<string> = new Set(['missing', 'none', 'rejected']);

/** Admin-only artist page section: the artist's discography releases missing from the library, requestable in place. */
export const ArtistRestOfDiscography: React.FC<ArtistRestOfDiscographyProps> = ({
  libraryArtistId,
  onRequestItem,
  onRequestDiscography,
  onViewInDiscover,
}) => {
  const target = useMemo(() => ({ libraryArtistId }), [libraryArtistId]);
  const { profile, isLoading, error, refetch } = useArtistProfile(target);
  const requests = useProfileRequests({ onRequest: onRequestItem, onRequestDiscography, onDone: refetch });
  const { requestItem, requestMany, busyIds } = requests;

  const releases = useMemo<ArtistProfileAlbum[]>(() => {
    if (!profile) return [];
    const { albums, singles_eps: singles, compilations } = profile.discography;
    return [...albums, ...singles, ...compilations].filter((a) => NOT_OWNED.has(a.status));
  }, [profile]);

  const requestable = useMemo(() => releases.filter((a) => ACTIONABLE.has(a.status)), [releases]);
  const artistName = profile?.artist.name ?? '';
  const discoveryId = profile?.artist.discovery_id ?? null;

  const handleRequestAll = useCallback((): void => {
    void requestMany(artistName, requestable.map(profileAlbumToDiscography));
  }, [requestMany, artistName, requestable]);

  return (
    <MachinedCard className="overflow-hidden" aria-label="Rest of discography">
      <div className="flex items-center gap-2 border-b border-[#1f1f1f] p-2 sm:p-3">
        <h3 className="min-w-0 flex-1 truncate font-mono text-xs font-bold uppercase tracking-widest text-[#e5a00d]">
          Rest of discography
          {!isLoading && profile && <span className="ml-2 text-neutral-500">{releases.length}</span>}
        </h3>
        {discoveryId && (
          <TapeDeckButton size="sm" className="shrink-0" onClick={() => onViewInDiscover(discoveryId)} icon={<Compass className="h-3.5 w-3.5" />}>
            View in Discover
          </TapeDeckButton>
        )}
        {requestable.length > 1 && (
          <TapeDeckButton
            size="sm"
            variant="amber"
            className="shrink-0"
            disabled={requests.bulkBusy}
            onClick={handleRequestAll}
            icon={requests.bulkBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
          >
            Request all
          </TapeDeckButton>
        )}
      </div>
      <div className="space-y-2 p-2 sm:p-3">
        {requests.error && <StatusMessage variant="error">{requests.error}</StatusMessage>}
        {isLoading ? (
          <div className="flex justify-center py-6">
            <Loader2 className="h-5 w-5 animate-spin text-[#e5a00d]" />
          </div>
        ) : error && !profile ? (
          <StatusMessage variant="error">{error}</StatusMessage>
        ) : releases.length === 0 ? (
          <p className="py-3 text-center font-mono text-xs text-neutral-500">
            {discoveryId ? 'Nothing missing: every known release is in the library.' : 'No matching discovery artist, so the wider discography is unknown.'}
          </p>
        ) : (
          <ul className="divide-y divide-[#1f1f1f] overflow-hidden rounded-[4px] border border-[#1f1f1f]">
            {releases.map((a) => {
              const meta = [releaseYear(a.release_date), a.record_type].filter(Boolean).join(' / ');
              return (
                <li key={a.id} className="flex items-center gap-2 p-2 sm:gap-3 sm:p-2.5">
                  {a.cover_url ? (
                    <img src={a.cover_url} alt="" width={40} height={40} loading="lazy" decoding="async" className="h-10 w-10 shrink-0 rounded-[3px] border border-[#222222] object-cover" />
                  ) : (
                    <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[3px] bg-[#141414]">
                      <Disc className="h-5 w-5 text-neutral-600" />
                    </div>
                  )}
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-neutral-200" title={a.title}>
                      {a.title}
                    </p>
                    {meta && <p className="truncate font-mono text-[11px] text-neutral-500">{meta}</p>}
                  </div>
                  <ProfileStatusBadge status={a.status} className="hidden sm:inline-flex" />
                  <ProfileRequestButton status={a.status} busy={busyIds.has(a.id)} onRequest={() => void requestItem({ ...profileAlbumToItem(a, a.status), album: a.title })} />
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </MachinedCard>
  );
};
