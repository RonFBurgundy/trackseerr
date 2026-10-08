import React, { useCallback, useEffect, useId, useState } from 'react';
import {
  ArrowLeft,
  Check,
  History,
  Loader2,
  Search,
} from 'lucide-react';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import type { MissingTrack, MediaTrackHit } from '@/services/missingService';
import { errorMessage } from '@/services/apiClient';

export interface MissingTracksModalProps {
  isOpen: boolean;
  onClose: () => void;
  playlistName?: string;
  tracks: MissingTrack[];
  onSearch: (query: string) => Promise<MediaTrackHit[]>;
  onMatch: (track: MissingTrack, hit: MediaTrackHit) => Promise<void>;
  onOpenOverrides?: () => void;
  overridesCount?: number;
}

function formatDuration(ms?: number | null): string {
  if (!ms || ms <= 0) return '';
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, '0')}`;
}

export const MissingTracksModal: React.FC<MissingTracksModalProps> = ({
  isOpen,
  onClose,
  playlistName,
  tracks,
  onSearch,
  onMatch,
  onOpenOverrides,
  overridesCount = 0,
}) => {
  const searchInputId = useId();
  const [matchingTrack, setMatchingTrack] = useState<MissingTrack | null>(null);
  const [query, setQuery] = useState<string>('');
  const [results, setResults] = useState<MediaTrackHit[]>([]);
  const [isSearching, setIsSearching] = useState<boolean>(false);
  const [isMatching, setIsMatching] = useState<boolean>(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [errorMessageText, setErrorMessageText] = useState<string | null>(null);

  const handleStartMatch = useCallback(
    async (track: MissingTrack) => {
      setMatchingTrack(track);
      const initialQuery = `${track.artist} ${track.title}`.trim();
      setQuery(initialQuery);
      setResults([]);
      setErrorMessageText(null);
      setStatusMessage(null);
      setIsSearching(true);
      try {
        const hits = await onSearch(initialQuery);
        setResults(hits);
      } catch (err: unknown) {
        setErrorMessageText(errorMessage(err, 'Failed to search library'));
      } finally {
        setIsSearching(false);
      }
    },
    [onSearch]
  );

  const handleSearchSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!query.trim()) return;
    setIsSearching(true);
    setErrorMessageText(null);
    try {
      const hits = await onSearch(query.trim());
      setResults(hits);
    } catch (err: unknown) {
      setErrorMessageText(errorMessage(err, 'Failed to search library'));
    } finally {
      setIsSearching(false);
    }
  };

  const handleSelectHit = async (hit: MediaTrackHit) => {
    if (!matchingTrack) return;
    setIsMatching(true);
    setErrorMessageText(null);
    try {
      await onMatch(matchingTrack, hit);
      setStatusMessage(`Matched "${matchingTrack.title}" to "${hit.title}"`);
      setMatchingTrack(null);
      setResults([]);
      setQuery('');
    } catch (err: unknown) {
      setErrorMessageText(errorMessage(err, 'Failed to record match override'));
    } finally {
      setIsMatching(false);
    }
  };

  // Reset matching state when modal closes
  useEffect(() => {
    if (!isOpen) {
      setMatchingTrack(null);
      setQuery('');
      setResults([]);
      setStatusMessage(null);
      setErrorMessageText(null);
    }
  }, [isOpen]);

  const footer = (
    <>
      {onOpenOverrides && (
        <TapeDeckButton
          size="sm"
          onClick={() => {
            onClose();
            onOpenOverrides();
          }}
          icon={<History className="h-3.5 w-3.5 text-[#e5a00d]" />}
          title="View and manage Match Memory overrides"
        >
          Match Memory ({overridesCount})
        </TapeDeckButton>
      )}
      <TapeDeckButton onClick={onClose}>Close</TapeDeckButton>
    </>
  );

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title={matchingTrack ? 'Match Track to Library' : 'Missing Playlist Tracks'}
      subtitle={
        matchingTrack
          ? `Finding a match for: ${matchingTrack.artist} - ${matchingTrack.title}`
          : playlistName
          ? `Playlist: ${playlistName}`
          : undefined
      }
      maxWidth="sm:max-w-3xl"
      footer={footer}
    >
      <div className="space-y-4">
        {errorMessageText && (
          <div
            role="alert"
            className="p-3 rounded-[3px] border border-[var(--status-error)]/50 bg-[var(--status-error)]/10 text-xs font-mono text-[var(--status-error)]"
          >
            {errorMessageText}
          </div>
        )}

        {statusMessage && (
          <div className="p-3 rounded-[3px] border border-[var(--status-success)]/40 bg-[var(--status-success)]/10 text-xs font-mono text-[var(--status-success)] flex items-center gap-2">
            <Check className="h-4 w-4" />
            <span>{statusMessage}</span>
          </div>
        )}

        {matchingTrack ? (
          /* Match Search Mode */
          <div className="space-y-4">
            <div className="flex items-center gap-2">
              <TapeDeckButton
                size="sm"
                onClick={() => {
                  setMatchingTrack(null);
                  setResults([]);
                }}
                icon={<ArrowLeft className="h-3.5 w-3.5" />}
              >
                Back to list
              </TapeDeckButton>
            </div>

            <form onSubmit={handleSearchSubmit} className="space-y-2">
              <label
                htmlFor={searchInputId}
                className="block text-xs uppercase font-mono tracking-wider text-neutral-300"
              >
                Search Plex Library
              </label>
              <div className="flex items-center gap-1.5">
                <input
                  id={searchInputId}
                  name="match-search-query"
                  type="search"
                  required
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Artist Title"
                  className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]"
                />
                <TapeDeckButton
                  type="submit"
                  variant="amber"
                  disabled={isSearching || !query.trim()}
                  icon={isSearching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
                >
                  Search
                </TapeDeckButton>
              </div>
            </form>

            {isSearching ? (
              <div className="flex flex-col items-center justify-center gap-2 py-10 text-xs font-mono uppercase tracking-widest text-[var(--text-secondary)]">
                <Loader2 className="h-5 w-5 animate-spin text-[var(--accent-amber)]" />
                <span>Searching Plex library...</span>
              </div>
            ) : results.length === 0 ? (
              <div className="py-8 text-center text-xs font-mono text-neutral-500">
                No matching tracks found in library for "{query}".
              </div>
            ) : (
              <div className="space-y-2">
                <p className="text-[11px] font-mono uppercase tracking-wider text-neutral-400">
                  Select matching track ({results.length} hit{results.length === 1 ? '' : 's'}):
                </p>
                <div
                  tabIndex={0}
                  role="region"
                  aria-label="Search results (scrollable)"
                  className="virtual-scroll overflow-y-auto border border-[#222222] rounded-[4px] bg-[#0d0d0d] divide-y divide-[#1c1c1c] max-h-72"
                >
                  {results.map((hit) => {
                    const durationStr = formatDuration(hit.duration);

                    return (
                      <div
                        key={hit.rating_key}
                        className="p-2.5 flex items-center justify-between gap-3 hover:bg-[#141414] transition-colors"
                      >
                        <div className="min-w-0 flex-1 space-y-0.5">
                          <p className="text-xs font-semibold text-white truncate">{hit.title}</p>
                          <p className="text-xs font-mono text-neutral-400 truncate">
                            {hit.artist}
                            {hit.album ? ` • ${hit.album}` : ''}
                          </p>
                          {durationStr && (
                            <span className="text-[10px] font-mono text-neutral-500">{durationStr}</span>
                          )}
                        </div>

                        <div className="shrink-0">
                          <TapeDeckButton
                            size="sm"
                            variant="amber"
                            disabled={isMatching}
                            onClick={() => void handleSelectHit(hit)}
                            icon={isMatching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                            title="Match this track"
                          >
                            Match
                          </TapeDeckButton>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        ) : (
          /* Missing Tracks List Mode */
          <div className="space-y-3">
            {tracks.length === 0 ? (
              <div className="py-12 text-center space-y-2">
                <Check className="h-8 w-8 text-[var(--status-success)] mx-auto opacity-70" />
                <p className="text-sm font-medium text-white">No Missing Tracks</p>
                <p className="text-xs text-neutral-400 font-mono">
                  All tracks in this playlist are currently present in your music library.
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                <div className="flex items-center justify-between text-xs font-mono text-neutral-400 px-1">
                  <span>
                    <strong className="text-white">{tracks.length}</strong> missing track
                    {tracks.length === 1 ? '' : 's'}
                  </span>
                </div>

                <div
                  tabIndex={0}
                  role="region"
                  aria-label="Missing tracks list (scrollable)"
                  className="virtual-scroll overflow-y-auto border border-[#222222] rounded-[4px] bg-[#0d0d0d] divide-y divide-[#1c1c1c] max-h-[55vh]"
                >
                  {tracks.map((track) => (
                    <div
                      key={track.id}
                      className="p-3 flex items-center justify-between gap-3 hover:bg-[#141414] transition-colors"
                    >
                      <div className="min-w-0 flex-1 space-y-0.5">
                        <p className="text-xs font-semibold text-white truncate">{track.title}</p>
                        <p className="text-xs font-mono text-neutral-400 truncate">
                          {track.artist}
                          {track.album ? ` • ${track.album}` : ''}
                        </p>
                        {track.lidarr_status && (
                          <span className="inline-block mt-0.5 text-[10px] font-mono uppercase px-1.5 py-0.2 bg-neutral-800 text-neutral-300 rounded-[2px]">
                            {track.lidarr_status}
                          </span>
                        )}
                      </div>

                      <div className="shrink-0">
                        <TapeDeckButton
                          size="sm"
                          variant="amber"
                          onClick={() => void handleStartMatch(track)}
                          icon={<Search className="h-3.5 w-3.5" />}
                          title={`Match ${track.title} to a library track`}
                          aria-label={`Match ${track.title}`}
                        >
                          Match
                        </TapeDeckButton>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </ObsidianModal>
  );
};
