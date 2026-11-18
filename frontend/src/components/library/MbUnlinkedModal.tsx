import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { ExternalLink, Link as LinkIcon, Loader2, Search } from 'lucide-react';
import type { MbUnlinkedAlbum, MbUnlinkedKind, StoredMatchSuggestion } from '@/types/models';
import { getMbUnlinked, getStoredSuggestions } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';
import { type FetchPage, useVirtualPagedList } from '@/hooks/useVirtualPagedList';
import { useMbSubmit } from '@/hooks/useMbSubmit';
import { summarizeApply, useStoredSuggestions } from '@/hooks/useStoredSuggestions';
import { ObsidianModal, TabStrip, TapeDeckButton } from '@/components/ui';
import { FlatList, type FlatListColumn } from '@/components/lists';
import { MatchSuggestionsPanel } from './MatchSuggestionsPanel';

export interface MbUnlinkedModalProps {
  isOpen: boolean;
  onClose: () => void;
  onOpenAlbum: (albumId: string) => void;
  /** Called after a match is applied so the admin can review the proposed file names and tags. */
  onReviewFiles: (albumId: string, albumTitle: string) => void;
  /** Called after a bulk apply so the admin can review file names across the whole library. */
  onReviewLibrary: () => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

export const MbUnlinkedModal: React.FC<MbUnlinkedModalProps> = ({
  isOpen,
  onClose,
  onOpenAlbum,
  onReviewFiles,
  onReviewLibrary,
  onToast,
}) => {
  const [kind, setKind] = useState<MbUnlinkedKind>('no_release_group');
  const [suggestedTab, setSuggestedTab] = useState<boolean>(false);
  const [canReviewLibrary, setCanReviewLibrary] = useState<boolean>(false);
  const [totals, setTotals] = useState<Record<MbUnlinkedKind | 'suggested', number | null>>({
    no_release_group: null,
    no_release: null,
    suggested: null,
  });
  const suggestions = useStoredSuggestions();

  const [linkingId, setLinkingId] = useState<string | null>(null);
  const [linkInput, setLinkInput] = useState<string>('');
  const [submittingId, setSubmittingId] = useState<string | null>(null);
  const [matchAlbum, setMatchAlbum] = useState<{ id: string; title: string } | null>(null);

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
    enabled: isOpen && !suggestedTab,
  });

  const fetchSuggestions: FetchPage<StoredMatchSuggestion> = useCallback(async ({ page, pageSize, signal }) => {
    const res = await getStoredSuggestions({ page, pageSize, signal });
    return { items: res.items, total: res.total };
  }, []);

  const suggestedList = useVirtualPagedList<StoredMatchSuggestion>(fetchSuggestions, {
    sortKey: '',
    sortDir: 'asc',
    getKey: (item) => item.album_id,
    enabled: isOpen && suggestedTab,
  });

  const { seedDefaults } = suggestions;
  useEffect(() => {
    seedDefaults(suggestedList.getLoadedItems());
  }, [seedDefaults, suggestedList, suggestedList.version]);

  useEffect(() => {
    if (!isOpen) {
      setMatchAlbum(null);
      setCanReviewLibrary(false);
    }
  }, [isOpen]);

  // Track counts on modal open for both tabs
  useEffect(() => {
    if (!isOpen) return;
    let active = true;
    Promise.all([
      getMbUnlinked({ page: 1, pageSize: 1, kind: 'no_release_group' }),
      getMbUnlinked({ page: 1, pageSize: 1, kind: 'no_release' }),
      getStoredSuggestions({ page: 1, pageSize: 1 }),
    ])
      .then(([rg, rel, sug]) => {
        if (!active) return;
        setTotals({
          no_release_group: rg.total,
          no_release: rel.total,
          suggested: sug.total,
        });
        setSuggestedTab(sug.total > 0);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [isOpen]);

  // Keep active kind total in sync with list.total
  useEffect(() => {
    if (isOpen && !suggestedTab) {
      setTotals((prev) => ({ ...prev, [kind]: list.total }));
    }
  }, [isOpen, suggestedTab, kind, list.total]);

  useEffect(() => {
    if (isOpen && suggestedTab) {
      setTotals((prev) => ({ ...prev, suggested: suggestedList.total }));
    }
  }, [isOpen, suggestedTab, suggestedList.total]);

  const reloadAll = useCallback(async () => {
    list.reload();
    suggestedList.reload();
    try {
      const [rg, rel, sug] = await Promise.all([
        getMbUnlinked({ page: 1, pageSize: 1, kind: 'no_release_group' }),
        getMbUnlinked({ page: 1, pageSize: 1, kind: 'no_release' }),
        getStoredSuggestions({ page: 1, pageSize: 1 }),
      ]);
      setTotals({ no_release_group: rg.total, no_release: rel.total, suggested: sug.total });
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Could not refresh the album counts'), 'error');
    }
  }, [list, suggestedList, onToast]);

  const handleApplySelected = useCallback(async () => {
    const summary = await suggestions.apply();
    if (!summary) return;
    const failedAll = summary.applied === 0 && summary.conflicts === 0;
    onToast(summarizeApply(summary), failedAll ? 'error' : 'ok');
    setCanReviewLibrary(summary.applied > 0);
    await reloadAll();
  }, [suggestions, onToast, reloadAll]);

  const suggestedColumns = useMemo<FlatListColumn<StoredMatchSuggestion>[]>(
    () => [
      {
        key: 'album',
        label: 'Album / Artist',
        sortable: false,
        width: 'minmax(0, 1.4fr)',
        mobile: 'title',
        render: (r) => (
          <div className="flex flex-col min-w-0">
            <span className="text-white font-medium block truncate">{r.album_title}</span>
            <span className="text-xs text-neutral-400 block truncate">{r.artist_name || 'Unknown Artist'}</span>
          </div>
        ),
      },
      {
        key: 'candidate',
        label: 'Suggested match',
        sortable: false,
        width: 'minmax(0, 1.6fr)',
        mobile: 'sub',
        render: (r) => {
          const year = r.candidate.first_release_date ? r.candidate.first_release_date.slice(0, 4) : '';
          const detail = [r.candidate.target_artist, [r.candidate.primary_type, year].filter(Boolean).join(' ')]
            .filter(Boolean)
            .join(', ');
          return (
            <span className="text-xs text-neutral-300 block truncate">
              &rarr; {r.candidate.title}
              {detail ? ` (${detail})` : ''}
            </span>
          );
        },
      },
      {
        key: 'confidence',
        label: 'Confidence',
        sortable: false,
        width: '90px',
        mobile: 'meta',
        render: (r) => <span className="text-xs text-[var(--accent-amber)] font-mono">{Math.round(r.confidence)}% sure</span>,
      },
      {
        key: 'files',
        label: 'Files',
        sortable: false,
        width: '130px',
        mobile: 'meta',
        render: (r) => (
          <span className="text-xs text-neutral-400 font-mono">
            {r.files_found} of {r.file_count} files found
          </span>
        ),
      },
    ],
    []
  );

  const suggestedRowActions = useCallback(
    (r: StoredMatchSuggestion) => (
      <TapeDeckButton
        size="sm"
        variant="amber"
        onClick={() => setMatchAlbum({ id: r.album_id, title: r.album_title })}
        icon={<Search className="h-3.5 w-3.5" />}
        title="Look at the other possible matches for this album"
      >
        Find match
      </TapeDeckButton>
    ),
    []
  );

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

  const handleMatchApplied = useCallback(
    (albumId: string, albumTitle: string) => {
      setMatchAlbum(null);
      void reloadAll();
      onReviewFiles(albumId, albumTitle);
    },
    [reloadAll, onReviewFiles]
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
            <div className="flex items-center gap-1.5 flex-wrap min-w-0">
              <button
                type="button"
                onClick={() => {
                  onClose();
                  onOpenAlbum(r.id);
                }}
                className="text-white hover:text-[var(--accent-amber)] font-medium text-left block truncate max-w-full transition-colors underline-offset-2 hover:underline"
                title="Open album details"
              >
                {r.title}
              </button>
              {r.year ? <span className="text-xs text-neutral-400 font-mono">({r.year})</span> : null}
            </div>
            <span className="text-xs text-neutral-400 block truncate">{r.artist_name || 'Unknown Artist'}</span>
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
            variant="amber"
            onClick={() => setMatchAlbum({ id: r.id, title: r.title })}
            icon={<Search className="h-3.5 w-3.5" />}
            title="Look for this album on MusicBrainz"
          >
            Find match
          </TapeDeckButton>
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
      {matchAlbum ? (
        <MatchSuggestionsPanel
          albumId={matchAlbum.id}
          albumTitle={matchAlbum.title}
          onBack={() => setMatchAlbum(null)}
          onApplied={handleMatchApplied}
          onSubmitToMusicBrainz={(id) => {
            setMatchAlbum(null);
            void handleSubmit(id);
          }}
          onToast={onToast}
        />
      ) : (
      <div className="flex flex-col gap-3 min-h-0">
        <p className="text-xs text-neutral-400 font-mono shrink-0">
          Opens MusicBrainz with this album prefilled. Log in there, check it, and submit; you&apos;ll come back here and the album gets linked.
        </p>

        <div className="shrink-0">
          <TabStrip aria-label="MusicBrainz unlinked categories">
            <TapeDeckButton
              size="sm"
              active={suggestedTab}
              onClick={() => {
                setSuggestedTab(true);
                setLinkingId(null);
                setLinkInput('');
              }}
            >
              Suggested
              {totals.suggested !== null ? ` (${totals.suggested})` : ''}
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              active={!suggestedTab && kind === 'no_release_group'}
              onClick={() => {
                setSuggestedTab(false);
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
              active={!suggestedTab && kind === 'no_release'}
              onClick={() => {
                setSuggestedTab(false);
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

        {suggestedTab && (
          <div className="shrink-0 flex flex-wrap items-center gap-1.5">
            <TapeDeckButton
              size="sm"
              disabled={suggestions.isApplying || suggestions.isSelectingAll || suggestedList.total === 0}
              onClick={() => void suggestions.selectAll()}
            >
              {suggestions.isSelectingAll ? <Loader2 className="h-3 w-3 animate-spin" /> : 'Select all'}
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              disabled={suggestions.isApplying || suggestions.selected.size === 0}
              onClick={suggestions.selectNone}
            >
              Select none
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              variant="amber"
              disabled={suggestions.isApplying || suggestions.selected.size === 0}
              onClick={() => void handleApplySelected()}
              title="Link every ticked album to its suggested MusicBrainz match"
            >
              {suggestions.isApplying ? <Loader2 className="h-3 w-3 animate-spin" /> : `Apply selected (${suggestions.selected.size})`}
            </TapeDeckButton>
            {canReviewLibrary && (
              <TapeDeckButton
                size="sm"
                onClick={() => {
                  onClose();
                  onReviewLibrary();
                }}
                title="Check the new file names and tags for the whole library"
              >
                Review file names
              </TapeDeckButton>
            )}
          </div>
        )}

        {suggestedTab ? (
          <div className="flex-1 min-h-0 relative">
            <FlatList<StoredMatchSuggestion>
              ariaLabel="Suggested MusicBrainz matches"
              columns={suggestedColumns}
              list={suggestedList}
              getKey={(r) => r.album_id}
              sortKey=""
              sortDir="asc"
              onSortChange={() => {}}
              hideMobileSortBar
              mobileLayout="compact"
              maxHeight="50dvh"
              selectedKeys={suggestions.selected}
              onSelectedKeysChange={(next) => suggestions.setSelected(new Set([...next].map(String)))}
              rowActions={suggestedRowActions}
              actionsLabel="Actions"
              actionsWidth="140px"
              emptyMessage="No suggestions yet. Run Library Self-Heal (Tasks page) to look up every unlinked album."
            />
          </div>
        ) : (
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
            mobileLayout="compact"
            maxHeight="55dvh"
            rowActions={rowActions}
            actionsLabel="Actions"
            actionsWidth="460px"
            emptyMessage={
              kind === 'no_release_group'
                ? 'No albums missing release groups.'
                : 'No albums missing editions.'
            }
          />
        </div>
        )}
      </div>
      )}
    </ObsidianModal>
  );
};
