import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { ExternalLink, Link as LinkIcon, Loader2 } from 'lucide-react';
import type { MbUnlinkedAlbum, MbUnlinkedKind } from '@/types/models';
import { getMbUnlinked } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';
import { type FetchPage, useVirtualPagedList } from '@/hooks/useVirtualPagedList';
import { useMbSubmit } from '@/hooks/useMbSubmit';
import { ObsidianModal, TabStrip, TapeDeckButton } from '@/components/ui';
import { FlatList, type FlatListColumn } from '@/components/lists';

export interface MbUnlinkedModalProps {
  isOpen: boolean;
  onClose: () => void;
  onOpenAlbum: (albumId: string) => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

export const MbUnlinkedModal: React.FC<MbUnlinkedModalProps> = ({
  isOpen,
  onClose,
  onOpenAlbum,
  onToast,
}) => {
  const [kind, setKind] = useState<MbUnlinkedKind>('no_release_group');
  const [totals, setTotals] = useState<Record<MbUnlinkedKind, number | null>>({
    no_release_group: null,
    no_release: null,
  });

  const [linkingId, setLinkingId] = useState<string | null>(null);
  const [linkInput, setLinkInput] = useState<string>('');
  const [submittingId, setSubmittingId] = useState<string | null>(null);

  const { submit, link, isLinking } = useMbSubmit();

  const fetchPage: FetchPage<MbUnlinkedAlbum> = useCallback(
    async ({ page, pageSize, filters, signal }) => {
      const k = (filters.kind as MbUnlinkedKind) || 'no_release_group';
      const res = await getMbUnlinked({ page, pageSize, kind: k, signal });
      return { items: res.items, total: res.total };
    },
    []
  );

  const list = useVirtualPagedList<MbUnlinkedAlbum>(fetchPage, {
    sortKey: '',
    sortDir: 'asc',
    filters: { kind },
    getKey: (item) => item.id,
    enabled: isOpen,
  });

  // Track counts on modal open for both tabs
  useEffect(() => {
    if (!isOpen) return;
    let active = true;
    Promise.all([
      getMbUnlinked({ page: 1, pageSize: 1, kind: 'no_release_group' }),
      getMbUnlinked({ page: 1, pageSize: 1, kind: 'no_release' }),
    ])
      .then(([rg, rel]) => {
        if (!active) return;
        setTotals({
          no_release_group: rg.total,
          no_release: rel.total,
        });
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [isOpen]);

  // Keep active kind total in sync with list.total
  useEffect(() => {
    if (isOpen) {
      setTotals((prev) => ({ ...prev, [kind]: list.total }));
    }
  }, [isOpen, kind, list.total]);

  const handleSubmit = useCallback(
    async (albumId: string) => {
      setSubmittingId(albumId);
      try {
        await submit(albumId);
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to open MusicBrainz seed'), 'error');
      } finally {
        setSubmittingId(null);
      }
    },
    [submit, onToast]
  );

  const handleSaveLink = useCallback(
    async (albumId: string) => {
      const val = linkInput.trim();
      if (!val) return;
      try {
        await link(albumId, val);
        list.removeItems(new Set([albumId]));
        setTotals((prev) => {
          const cur = prev[kind];
          return cur !== null ? { ...prev, [kind]: Math.max(0, cur - 1) } : prev;
        });
        setLinkingId(null);
        setLinkInput('');
        onToast('Linked to MusicBrainz release', 'ok');
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to link MusicBrainz release'), 'error');
      }
    },
    [linkInput, link, list, kind, onToast]
  );

  const handleCancelLink = useCallback(() => {
    setLinkingId(null);
    setLinkInput('');
  }, []);

  const columns = useMemo<FlatListColumn<MbUnlinkedAlbum>[]>(
    () => [
      {
        key: 'title',
        label: 'Album / Artist',
        sortable: false,
        width: 'minmax(0, 1.8fr)',
        mobile: 'title',
        render: (r) => (
          <div className="flex flex-col min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap">
              <button
                type="button"
                onClick={() => {
                  onClose();
                  onOpenAlbum(r.id);
                }}
                className="text-white hover:text-[var(--accent-amber)] font-medium text-left truncate transition-colors underline-offset-2 hover:underline"
                title="Open album details"
              >
                {r.title}
              </button>
              {r.year ? <span className="text-xs text-neutral-400 font-mono">({r.year})</span> : null}
            </div>
            <span className="text-xs text-neutral-400 truncate">{r.artist_name || 'Unknown Artist'}</span>
          </div>
        ),
      },
      {
        key: 'tracks_files',
        label: 'Tracks / Files',
        sortable: false,
        width: '130px',
        mobile: 'meta',
        render: (r) => (
          <span className="text-xs text-neutral-400 font-mono">
            {r.track_count} trk / {r.file_count} {r.file_count === 1 ? 'file' : 'files'}
          </span>
        ),
      },
    ],
    [onClose, onOpenAlbum]
  );

  const rowActions = useCallback(
    (r: MbUnlinkedAlbum) => {
      const isThisLinking = linkingId === r.id;
      const isThisSubmitting = submittingId === r.id;

      if (isThisLinking) {
        const inputId = `mb-link-input-${r.id}`;
        return (
          <div className="flex items-center gap-1.5 flex-wrap">
            <label htmlFor={inputId} className="sr-only">
              MusicBrainz Release MBID or URL
            </label>
            <input
              id={inputId}
              name={`release_mbid_${r.id}`}
              type="text"
              placeholder="MBID or URL"
              value={linkInput}
              onChange={(e) => setLinkInput(e.target.value)}
              disabled={isLinking}
              autoFocus
              onKeyDown={(e) => {
                if (e.key === 'Enter') void handleSaveLink(r.id);
                if (e.key === 'Escape') handleCancelLink();
              }}
              className="h-8 px-2 text-xs bg-[#0a0a0a] border border-[#2a2a2a] focus:border-[#e5a00d] focus:outline-none rounded-[3px] text-white font-mono w-44 sm:w-56"
            />
            <TapeDeckButton
              size="sm"
              variant="amber"
              disabled={isLinking || !linkInput.trim()}
              onClick={() => void handleSaveLink(r.id)}
              title="Save MusicBrainz release link"
            >
              {isLinking ? <Loader2 className="h-3 w-3 animate-spin" /> : 'Save'}
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              disabled={isLinking}
              onClick={handleCancelLink}
              title="Cancel linking"
            >
              Cancel
            </TapeDeckButton>
          </div>
        );
      }

      return (
        <div className="flex items-center gap-1.5 flex-wrap">
          <TapeDeckButton
            size="sm"
            disabled={isThisSubmitting}
            onClick={() => void handleSubmit(r.id)}
            icon={
              isThisSubmitting ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <ExternalLink className="h-3.5 w-3.5" />
              )
            }
            title="Opens MusicBrainz with this album prefilled"
          >
            Submit to MusicBrainz
          </TapeDeckButton>
          <TapeDeckButton
            size="sm"
            onClick={() => {
              setLinkingId(r.id);
              setLinkInput('');
            }}
            icon={<LinkIcon className="h-3.5 w-3.5" />}
            title="Link to an existing MusicBrainz release"
          >
            Link&hellip;
          </TapeDeckButton>
          <TapeDeckButton
            size="sm"
            onClick={() => {
              onClose();
              onOpenAlbum(r.id);
            }}
            title="Open album details"
          >
            Open
          </TapeDeckButton>
        </div>
      );
    },
    [
      linkingId,
      submittingId,
      linkInput,
      isLinking,
      handleSubmit,
      handleSaveLink,
      handleCancelLink,
      onClose,
      onOpenAlbum,
    ]
  );

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="Not on MusicBrainz"
      maxWidth="sm:max-w-4xl"
    >
      <div className="flex flex-col gap-3 min-h-0">
        <p className="text-xs text-neutral-400 font-mono shrink-0">
          Opens MusicBrainz with this album prefilled. Log in there, check it, and submit; you&apos;ll come back here and the album gets linked.
        </p>

        <div className="shrink-0">
          <TabStrip aria-label="MusicBrainz unlinked categories">
            <TapeDeckButton
              size="sm"
              active={kind === 'no_release_group'}
              onClick={() => {
                setKind('no_release_group');
                setLinkingId(null);
                setLinkInput('');
              }}
            >
              Not on MusicBrainz
              {totals.no_release_group !== null ? ` (${totals.no_release_group})` : ''}
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              active={kind === 'no_release'}
              onClick={() => {
                setKind('no_release');
                setLinkingId(null);
                setLinkInput('');
              }}
            >
              Missing edition
              {totals.no_release !== null ? ` (${totals.no_release})` : ''}
            </TapeDeckButton>
          </TabStrip>
        </div>

        <div className="flex-1 min-h-0 relative">
          <FlatList<MbUnlinkedAlbum>
            ariaLabel="MusicBrainz unlinked albums"
            columns={columns}
            list={list}
            getKey={(r) => r.id}
            sortKey=""
            sortDir="asc"
            onSortChange={() => {}}
            hideMobileSortBar
            maxHeight="55dvh"
            rowActions={rowActions}
            actionsLabel="Actions"
            actionsWidth="340px"
            emptyMessage={
              kind === 'no_release_group'
                ? 'No albums missing release groups.'
                : 'No albums missing editions.'
            }
          />
        </div>
      </div>
    </ObsidianModal>
  );
};
