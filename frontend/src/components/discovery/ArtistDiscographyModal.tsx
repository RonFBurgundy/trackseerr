import React, { useEffect, useState } from 'react';
import { Disc, Loader2, Plus } from 'lucide-react';
import { ObsidianModal, StatusMessage, TapeDeckButton } from '@/components/ui';
import type { ArtistDetail, ArtistDiscographyAlbum, DiscoveryItem } from '@/types/models';
import { getDiscoveryArtistDetail } from '@/services/discoveryService';
import { MAX_BATCH_ITEMS } from '@/services/requestService';
import { errorMessage } from '@/services/apiClient';

export interface ArtistDiscographyModalProps {
  artist: DiscoveryItem | null;
  onClose: () => void;
  onRequestDiscography: (artist: string, albums: ArtistDiscographyAlbum[]) => Promise<void>;
}

/** Studio albums first, then singles and EPs; compilations are excluded. Capped at the batch limit. */
export function selectDiscography(detail: ArtistDetail): {
  included: ArtistDiscographyAlbum[];
  total: number;
} {
  const all = [...(detail.albums ?? []), ...(detail.singles_eps ?? [])];
  return { included: all.slice(0, MAX_BATCH_ITEMS), total: all.length };
}

export const ArtistDiscographyModal: React.FC<ArtistDiscographyModalProps> = ({
  artist,
  onClose,
  onRequestDiscography,
}) => {
  const [detail, setDetail] = useState<ArtistDetail | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [isRequesting, setIsRequesting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<boolean>(false);

  const artistId = artist?.id;
  useEffect(() => {
    if (!artistId) return;
    let cancelled = false;
    setDetail(null);
    setError(null);
    setDone(false);
    setIsLoading(true);
    getDiscoveryArtistDetail(artistId)
      .then((res) => {
        if (!cancelled) setDetail(res);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorMessage(err, 'Failed to load discography'));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [artistId]);

  if (!artist) return null;

  const { included, total } = detail ? selectDiscography(detail) : { included: [], total: 0 };
  const artistName = detail?.name || artist.artist || artist.title;

  const handleRequest = async () => {
    setIsRequesting(true);
    setError(null);
    try {
      await onRequestDiscography(artistName, included);
      setDone(true);
    } catch (err: unknown) {
      // Quota (400) and duplicate (409) messages come straight from the server.
      setError(errorMessage(err, 'Failed to request discography'));
    } finally {
      setIsRequesting(false);
    }
  };

  return (
    <ObsidianModal
      isOpen
      onClose={onClose}
      title={artistName}
      subtitle="Discography"
      footer={
        <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between w-full gap-2 sm:gap-3">
          <span className="text-xs text-[var(--text-secondary)] font-mono text-center sm:text-left">
            {included.length} release{included.length === 1 ? '' : 's'}
          </span>
          <TapeDeckButton
            variant="amber"
            disabled={included.length === 0 || isRequesting || done}
            onClick={handleRequest}
            icon={
              isRequesting ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Plus className="h-4 w-4" />
              )
            }
          >
            {done ? 'Discography requested' : 'Request discography'}
          </TapeDeckButton>
        </div>
      }
    >
      <div className="space-y-4">
        {error && <StatusMessage variant="error">{error}</StatusMessage>}
        {done && (
          <StatusMessage variant="success">
            Requested {included.length} releases as one discography request.
          </StatusMessage>
        )}
        {total > MAX_BATCH_ITEMS && (
          <StatusMessage variant="info">
            Only the first {MAX_BATCH_ITEMS} of {total} releases fit in one request.
          </StatusMessage>
        )}
        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-6 w-6 text-[var(--accent-amber)] animate-spin" />
          </div>
        ) : included.length > 0 ? (
          <ul className="divide-y divide-[var(--border-subtle)] border border-[var(--border-subtle)] rounded-[4px] overflow-hidden">
            {included.map((a) => (
              <li key={a.id} className="flex items-center gap-3 p-2.5">
                {a.cover_url ? (
                  <img
                    src={a.cover_url}
                    alt=""
                    loading="lazy"
                    className="h-10 w-10 rounded-[3px] object-cover border border-[var(--border-subtle)]"
                  />
                ) : (
                  <div className="h-10 w-10 flex items-center justify-center bg-[var(--bg-card)] rounded-[3px]">
                    <Disc className="h-5 w-5 text-[var(--text-muted)]" />
                  </div>
                )}
                <div className="min-w-0">
                  <p className="text-sm text-[var(--text-primary)] truncate">{a.title}</p>
                  <p className="text-[11px] font-mono text-[var(--text-muted)]">
                    {[a.record_type, a.release_date?.slice(0, 4)].filter(Boolean).join(' / ')}
                  </p>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          !error && (
            <p className="text-xs text-[var(--text-muted)] font-mono py-4">
              No releases found for this artist.
            </p>
          )
        )}
      </div>
    </ObsidianModal>
  );
};
