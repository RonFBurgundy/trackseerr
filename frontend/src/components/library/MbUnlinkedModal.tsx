import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ExternalLink, FileCheck, Link as LinkIcon, Loader2, RefreshCw, Search } from 'lucide-react';
import { useVirtualizer } from '@tanstack/react-virtual';
import type { MbUnlinkedAlbum, MbUnlinkedKind, StoredMatchSuggestion } from '@/types/models';
import { getMbUnlinked, getStoredSuggestions } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';
import { type FetchPage, useVirtualPagedList } from '@/hooks/useVirtualPagedList';
import { useMbSubmit } from '@/hooks/useMbSubmit';
import { SURE_CONFIDENCE, summarizeApply, useStoredSuggestions } from '@/hooks/useStoredSuggestions';
import { ObsidianModal, SelectionCheckbox, TapeDeckButton } from '@/components/ui';
import { CHIP_BASE, CHIP_RISKY, CHIP_SAFE } from './libraryChips';
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

  const activeList = suggestedTab ? suggestedList : list;
  const parentRef = useRef<HTMLDivElement | null>(null);

  const rowVirtualizer = useVirtualizer({
    count: activeList.total,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 88,
    overscan: 8,
  });

  const virtualItems = rowVirtualizer.getVirtualItems();
  const firstIndex = virtualItems.length > 0 ? virtualItems[0].index : -1;
  const lastIndex = virtualItems.length > 0 ? virtualItems[virtualItems.length - 1].index : -1;

  useEffect(() => {
    if (!isOpen || matchAlbum) return;
    if (firstIndex >= 0) {
      activeList.ensureRange(firstIndex, lastIndex);
    }
  }, [isOpen, matchAlbum, firstIndex, lastIndex, activeList, activeList.total, activeList.version]);

  const allSuggestedSelected =
    suggestedList.total > 0 && suggestions.selected.size === suggestedList.total;

  const footer = (
    <>
      <TapeDeckButton onClick={onClose}>Close</TapeDeckButton>
      {!matchAlbum && suggestedTab && canReviewLibrary && (
        <TapeDeckButton
          onClick={() => {
            onClose();
            onReviewLibrary();
          }}
          title="Check the new file names and tags for the whole library"
        >
          Review file names
        </TapeDeckButton>
      )}
      {!matchAlbum && suggestedTab && (
        <TapeDeckButton
          variant="amber"
          disabled={suggestions.isApplying || suggestions.selected.size === 0}
          onClick={() => void handleApplySelected()}
          title="Link every ticked album to its suggested MusicBrainz match"
          icon={
            suggestions.isApplying ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <LinkIcon className="h-4 w-4" />
            )
          }
        >
          {suggestions.isApplying ? 'Linking...' : `Link selected (${suggestions.selected.size})`}
        </TapeDeckButton>
      )}
    </>
  );

  const renderSuggestedRow = (r: StoredMatchSuggestion) => {
    const isSelected = suggestions.selected.has(r.album_id);
    const year = r.candidate.first_release_date ? r.candidate.first_release_date.slice(0, 4) : '';
    const detail = [r.candidate.target_artist, [r.candidate.primary_type, year].filter(Boolean).join(' ')]
      .filter(Boolean)
      .join(', ');

    return (
      <>
        <div className="pt-0.5 shrink-0">
          <SelectionCheckbox
            name={`mb-suggest-${r.album_id}`}
            checked={isSelected}
            onChange={() => {
              const next = new Set(suggestions.selected);
              if (isSelected) next.delete(r.album_id);
              else next.add(r.album_id);
              suggestions.setSelected(next);
            }}
            label={`Select ${r.album_title}`}
            inline
          />
        </div>
        <div className="min-w-0 flex-1 space-y-1">
          <p className="text-sm font-medium text-white truncate">{r.album_title}</p>
          <p className="text-xs text-neutral-400 truncate">{r.artist_name || 'Unknown Artist'}</p>
          <p className="font-mono text-[11px] text-[#e5a00d]/80 truncate">
            &rarr; {r.candidate.title}
            {detail ? ` (${detail})` : ''}
          </p>
          <div className="flex flex-wrap gap-1.5 pt-0.5">
            <TapeDeckButton
              size="sm"
              variant="amber"
              onClick={() => setMatchAlbum({ id: r.album_id, title: r.album_title })}
              icon={<Search className="h-3.5 w-3.5" />}
              title="Look at the other possible matches for this album"
            >
              Find match
            </TapeDeckButton>
          </div>
          <div className="tab-strip mt-1 h-4 overflow-x-auto">
            <span className={`${CHIP_BASE} ${r.confidence >= SURE_CONFIDENCE ? CHIP_SAFE : CHIP_RISKY}`}>
              {Math.round(r.confidence)}% sure
            </span>
            <span className={`${CHIP_BASE} ${CHIP_SAFE}`}>
              {r.files_found} of {r.file_count} files found
            </span>
          </div>
        </div>
      </>
    );
  };

  const renderUnlinkedRow = (r: MbUnlinkedAlbum) => {
    const isThisLinking = linkingId === r.id;
    const isThisSubmitting = submittingId === r.id;
    const inputId = `mb-link-input-${r.id}`;

    return (
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex items-center gap-1.5 flex-wrap min-w-0">
          <button
            type="button"
            onClick={() => {
              onClose();
              onOpenAlbum(r.id);
            }}
            className="text-sm text-white hover:text-[var(--accent-amber)] font-medium text-left block truncate max-w-full transition-colors underline-offset-2 hover:underline"
            title="Open album details"
          >
            {r.title}
          </button>
          {r.year ? <span className="text-xs text-neutral-400 font-mono">({r.year})</span> : null}
        </div>
        <p className="text-xs text-neutral-400 truncate">{r.artist_name || 'Unknown Artist'}</p>
        <div className="tab-strip mt-1 h-4 overflow-x-auto">
          <span className={`${CHIP_BASE} ${CHIP_SAFE}`}>{r.track_count} tracks</span>
          <span className={`${CHIP_BASE} ${CHIP_SAFE}`}>{r.file_count} files</span>
        </div>
        <div className="flex flex-wrap gap-1.5 pt-0.5">
          {isThisLinking ? (
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
          ) : (
            <>
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
            </>
          )}
        </div>
      </div>
    );
  };

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="Not on MusicBrainz"
      subtitle="Albums that aren't linked to a MusicBrainz release yet"
      maxWidth="sm:max-w-4xl"
      footer={footer}
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
        <div className="space-y-3">
          <p className="text-xs font-mono text-neutral-400">
            {suggestedTab
              ? 'Matches Library Self-Heal found. Sure matches (80% or more) start ticked.'
              : "Opens MusicBrainz with this album prefilled. Log in there, check it, and submit; you'll come back here and the album gets linked."}
          </p>

          {/* View filter bar */}
          <div
            role="group"
            aria-label="MusicBrainz unlinked categories"
            className="tab-strip overflow-x-auto pb-0.5"
          >
            <button
              type="button"
              aria-pressed={suggestedTab}
              onClick={() => {
                setSuggestedTab(true);
                setLinkingId(null);
                setLinkInput('');
              }}
              className={`${CHIP_BASE} cursor-pointer transition-colors ${
                suggestedTab
                  ? 'border-[#e5a00d] bg-[#e5a00d] text-black font-bold'
                  : `${CHIP_SAFE} hover:text-white hover:border-[#3a3a3a]`
              }`}
            >
              Suggested{totals.suggested !== null ? ` · ${totals.suggested}` : ''}
            </button>
            <button
              type="button"
              aria-pressed={!suggestedTab && kind === 'no_release_group'}
              onClick={() => {
                setSuggestedTab(false);
                setKind('no_release_group');
                setLinkingId(null);
                setLinkInput('');
              }}
              className={`${CHIP_BASE} cursor-pointer transition-colors ${
                !suggestedTab && kind === 'no_release_group'
                  ? 'border-[#e5a00d] bg-[#e5a00d] text-black font-bold'
                  : `${CHIP_SAFE} hover:text-white hover:border-[#3a3a3a]`
              }`}
            >
              Not on MusicBrainz{totals.no_release_group !== null ? ` · ${totals.no_release_group}` : ''}
            </button>
            <button
              type="button"
              aria-pressed={!suggestedTab && kind === 'no_release'}
              onClick={() => {
                setSuggestedTab(false);
                setKind('no_release');
                setLinkingId(null);
                setLinkInput('');
              }}
              className={`${CHIP_BASE} cursor-pointer transition-colors ${
                !suggestedTab && kind === 'no_release'
                  ? 'border-[#e5a00d] bg-[#e5a00d] text-black font-bold'
                  : `${CHIP_SAFE} hover:text-white hover:border-[#3a3a3a]`
              }`}
            >
              Missing edition{totals.no_release !== null ? ` · ${totals.no_release}` : ''}
            </button>
          </div>

          {/* Header Toolbar */}
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#222222] pb-2 text-xs font-mono">
            {suggestedTab ? (
              <>
                <div className="flex items-center gap-2">
                  <SelectionCheckbox
                    name="mb-suggested-select-all"
                    checked={allSuggestedSelected}
                    onChange={() => {
                      if (allSuggestedSelected) suggestions.selectNone();
                      else void suggestions.selectAll();
                    }}
                    label="Select all"
                    inline
                  />
                  <span className="text-neutral-300">Select all</span>
                  <span className="text-neutral-500">•</span>
                  <button
                    type="button"
                    onClick={() => void suggestions.selectSure()}
                    title="Select only sure matches (80% or more)"
                    className="text-neutral-400 hover:text-white transition-colors"
                  >
                    Select sure only
                  </button>
                  <span className="text-neutral-500">•</span>
                  <button
                    type="button"
                    onClick={() => void suggestions.selectAll()}
                    className="text-neutral-400 hover:text-white transition-colors"
                  >
                    All
                  </button>
                  <span className="text-neutral-500">•</span>
                  <button
                    type="button"
                    onClick={suggestions.selectNone}
                    className="text-neutral-400 hover:text-white transition-colors"
                  >
                    None
                  </button>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-neutral-400">
                  <span className="inline-flex items-center gap-1.5">
                    {suggestions.isSelectingAll && (
                      <Loader2 className="h-3 w-3 animate-spin text-[var(--accent-amber)]" />
                    )}
                    <span>
                      <strong className="text-white">{suggestions.selected.size}</strong> of {suggestedList.total} selected
                    </span>
                  </span>
                  <TapeDeckButton
                    size="sm"
                    onClick={() => void reloadAll()}
                    icon={<RefreshCw className="h-3 w-3" />}
                    title="Reload suggestions"
                  >
                    Refresh
                  </TapeDeckButton>
                </div>
              </>
            ) : (
              <div className="ml-auto flex flex-wrap items-center gap-2 text-neutral-400">
                <span>
                  <strong className="text-white">{list.total}</strong> albums
                </span>
                <TapeDeckButton
                  size="sm"
                  onClick={() => void reloadAll()}
                  icon={<RefreshCw className="h-3 w-3" />}
                  title="Reload albums"
                >
                  Refresh
                </TapeDeckButton>
              </div>
            )}
          </div>

          {suggestedTab && suggestions.error && (
            <p role="alert" className="text-xs font-mono text-[var(--status-error)]">
              {suggestions.error}
            </p>
          )}

          {activeList.error && (
            <div
              role="alert"
              className="flex flex-wrap items-center justify-between gap-2 text-xs font-mono text-[var(--status-error)]"
            >
              <span>{activeList.error}</span>
              <TapeDeckButton size="sm" onClick={activeList.retry}>
                Try again
              </TapeDeckButton>
            </div>
          )}

          {activeList.loading && activeList.total === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 py-14 text-xs font-mono uppercase tracking-widest text-[var(--text-secondary)]">
              <Loader2 className="h-6 w-6 animate-spin text-[var(--accent-amber)]" />
              <span>Loading albums...</span>
            </div>
          ) : activeList.total === 0 && !activeList.error ? (
            <div className="py-12 text-center space-y-2">
              <FileCheck className="h-8 w-8 text-[var(--accent-amber)] mx-auto opacity-70" />
              <p className="text-sm font-medium text-white">
                {suggestedTab
                  ? 'No suggestions yet.'
                  : kind === 'no_release_group'
                    ? 'No albums missing release groups.'
                    : 'No albums missing editions.'}
              </p>
              {suggestedTab && (
                <p className="text-xs text-neutral-400 font-mono">
                  Run Library Self-Heal (Tasks page) to look up every unlinked album.
                </p>
              )}
            </div>
          ) : (
            <div
              ref={parentRef}
              tabIndex={0}
              role="region"
              aria-label={suggestedTab ? 'Suggested MusicBrainz matches (scrollable)' : 'MusicBrainz unlinked albums (scrollable)'}
              className="virtual-scroll overflow-y-auto border border-[#222222] rounded-[4px] bg-[#0d0d0d] divide-y divide-[#1c1c1c]"
              style={{ maxHeight: 'min(450px, 55vh)' }}
            >
              <div
                style={{
                  height: `${rowVirtualizer.getTotalSize()}px`,
                  width: '100%',
                  position: 'relative',
                }}
              >
                {virtualItems.map((virtualRow) => {
                  const suggestedItem = suggestedTab ? suggestedList.getItem(virtualRow.index) : undefined;
                  const unlinkedItem = suggestedTab ? undefined : list.getItem(virtualRow.index);
                  const isSelected = suggestedItem !== undefined && suggestions.selected.has(suggestedItem.album_id);
                  const rowKey = suggestedTab
                    ? (suggestedItem ? suggestedItem.album_id : `placeholder-${virtualRow.index}`)
                    : (unlinkedItem ? unlinkedItem.id : `placeholder-${virtualRow.index}`);
                  const rowContent = suggestedTab
                    ? (suggestedItem ? renderSuggestedRow(suggestedItem) : <div className="text-xs font-mono text-neutral-500">Loading&hellip;</div>)
                    : (unlinkedItem ? renderUnlinkedRow(unlinkedItem) : <div className="text-xs font-mono text-neutral-500">Loading&hellip;</div>);

                  return (
                    <div
                      key={rowKey}
                      data-index={virtualRow.index}
                      ref={rowVirtualizer.measureElement}
                      style={{
                        position: 'absolute',
                        top: 0,
                        left: 0,
                        width: '100%',
                        transform: `translateY(${virtualRow.start}px)`,
                      }}
                      className={`p-2.5 transition-colors flex items-start gap-2.5 ${
                        isSelected ? 'bg-[#181818]' : 'hover:bg-[#141414]'
                      }`}
                    >
                      {rowContent}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </ObsidianModal>
  );
};
