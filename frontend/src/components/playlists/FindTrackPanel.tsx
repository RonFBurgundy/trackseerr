import React, { useId, useState } from 'react';
import { ArrowLeft, Check, Loader2, Search } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';
import { isOwnedStatus } from '@/components/discovery/profileItems';
import { useFindTrack } from '@/hooks/useFindTrack';
import { errorMessage } from '@/services/apiClient';
import type { DiscoveryItem } from '@/types/models';
import { isDiscoveryStatus } from '@/types/models';

export interface FindTrackPanelProps {
  playlistId: string;
  playlistName: string;
  /** The playlist track being fixed, as the source playlist credits it. */
  track: { title: string; artist: string };
  onBack: () => void;
  /** Request a catalog song that is not in the library yet. */
  onRequest: (item: DiscoveryItem) => Promise<void>;
  /** A suggestion or report went through: the modal reloads its tracks. */
  onDone: () => void;
}

const fieldClass =
  'w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]';
const labelClass = 'block text-xs uppercase font-mono tracking-wider text-neutral-300';

/** Strips runs of asterisks (censored words) so the prefilled search can still match. */
export function searchPrefill(artist: string, title: string): string {
  return `${artist} ${title}`.replace(/\*+/g, ' ').replace(/\s+/g, ' ').trim();
}

/** Inline panel of the tracks modal: find the right catalog song for a missing track, or report it. */
export const FindTrackPanel: React.FC<FindTrackPanelProps> = ({ playlistId, playlistName, track, onBack, onRequest, onDone }) => {
  const ids = useId();
  const [query, setQuery] = useState<string>(() => searchPrefill(track.artist, track.title));
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [note, setNote] = useState<string>('');
  const [reporting, setReporting] = useState<boolean>(false);
  const [reportNote, setReportNote] = useState<string>('');
  const [requested, setRequested] = useState<ReadonlySet<string>>(new Set());
  const [requestError, setRequestError] = useState<string | null>(null);
  const fix = useFindTrack(playlistId, playlistName, track);

  const submitSearch = (e: React.FormEvent): void => {
    e.preventDefault();
    setConfirmId(null);
    void fix.runSearch(query);
  };

  const confirm = async (item: DiscoveryItem): Promise<void> => {
    if (await fix.suggest(item, note)) onDone();
  };

  const request = async (item: DiscoveryItem): Promise<void> => {
    setRequestError(null);
    try {
      await onRequest(item);
      setRequested((prev) => new Set([...prev, item.id]));
    } catch (err: unknown) {
      setRequestError(errorMessage(err, 'Request failed'));
    }
  };

  const sendReport = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault();
    if (await fix.report(reportNote)) onDone();
  };

  return (
    <div className="flex flex-col gap-3 min-h-0">
      <div className="flex items-center gap-2">
        <TapeDeckButton size="sm" onClick={onBack} icon={<ArrowLeft className="h-3.5 w-3.5" />}>
          Back to tracks
        </TapeDeckButton>
        <p className="min-w-0 truncate text-xs font-mono text-neutral-400" title={`${track.artist} - ${track.title}`}>
          {track.artist} - {track.title}
        </p>
      </div>

      {(fix.error || requestError) && (
        <div role="alert" className="p-3 rounded-[3px] border border-[var(--status-error)]/50 bg-[var(--status-error)]/10 text-xs font-mono text-[var(--status-error)]">
          {fix.error ?? requestError}
        </div>
      )}
      {fix.notice && (
        <div className="p-3 rounded-[3px] border border-[var(--status-success)]/40 bg-[var(--status-success)]/10 text-xs font-mono text-[var(--status-success)] flex items-center gap-2">
          <Check className="h-4 w-4" />
          <span>{fix.notice}</span>
        </div>
      )}

      {!reporting && (
        <>
          <form onSubmit={submitSearch} className="space-y-2">
            <label htmlFor={`${ids}-query`} className={labelClass}>
              Search the catalog
            </label>
            <div className="flex items-center gap-1.5">
              <input
                id={`${ids}-query`}
                name="find-track-query"
                type="search"
                required
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                className={fieldClass}
              />
              <TapeDeckButton type="submit" size="sm" variant="amber" disabled={fix.searching} aria-label="Search" icon={fix.searching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />} />
            </div>
          </form>

          {fix.searched && fix.results.length === 0 && (
            <p className="py-4 text-center text-xs text-neutral-500 font-mono">No songs found.</p>
          )}

          {fix.results.length > 0 && (
            <ul className="flex flex-col gap-1">
              {fix.results.map((item) => {
                const owned = isDiscoveryStatus(item.status) && isOwnedStatus(item.status);
                const isRequested = requested.has(item.id);
                return (
                  <li key={item.id} className="rounded-[3px] border border-[#222222] bg-[#141414] px-2.5 py-2">
                    <div className="flex items-center gap-3">
                      {item.cover_url ? (
                        <img src={item.cover_url} alt="" width={40} height={40} loading="lazy" decoding="async" className="h-10 w-10 shrink-0 rounded-[3px] border border-[#222222] object-cover" />
                      ) : (
                        <div className="h-10 w-10 shrink-0 rounded-[3px] border border-[#222222] bg-[#0d0d0d]" aria-hidden="true" />
                      )}
                      <div className="min-w-0 flex-1">
                        <p className="text-sm text-white truncate" title={item.title}>
                          {item.title}
                        </p>
                        <p className="text-xs text-neutral-400 truncate">{[item.artist, item.album].filter(Boolean).join(' · ')}</p>
                      </div>
                      {owned ? (
                        <TapeDeckButton size="sm" variant="amber" disabled={fix.submitting} onClick={() => setConfirmId(confirmId === item.id ? null : item.id)}>
                          This is it
                        </TapeDeckButton>
                      ) : (
                        <TapeDeckButton size="sm" disabled={isRequested} onClick={() => void request(item)}>
                          {isRequested ? 'Requested' : 'Request it'}
                        </TapeDeckButton>
                      )}
                    </div>
                    {owned && confirmId === item.id && (
                      <div className="mt-2 space-y-2">
                        <label htmlFor={`${ids}-note`} className={labelClass}>
                          Note for the admin (optional)
                        </label>
                        <textarea
                          id={`${ids}-note`}
                          name="find-track-note"
                          rows={2}
                          value={note}
                          onChange={(e) => setNote(e.target.value)}
                          className={fieldClass}
                        />
                        <TapeDeckButton size="sm" variant="amber" disabled={fix.submitting} onClick={() => void confirm(item)} icon={fix.submitting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}>
                          Confirm match
                        </TapeDeckButton>
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}

          <button
            type="button"
            onClick={() => setReporting(true)}
            className="self-start text-xs font-mono text-[var(--accent-amber)] hover:underline"
          >
            Can&apos;t find it? Report to admin
          </button>
        </>
      )}

      {reporting && (
        <form onSubmit={(e) => void sendReport(e)} className="space-y-2">
          <label htmlFor={`${ids}-report`} className={labelClass}>
            What should this track match?
          </label>
          <textarea
            id={`${ids}-report`}
            name="find-track-report"
            rows={3}
            value={reportNote}
            onChange={(e) => setReportNote(e.target.value)}
            className={fieldClass}
          />
          <div className="flex gap-2">
            <TapeDeckButton type="submit" size="sm" variant="amber" disabled={fix.submitting} icon={fix.submitting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}>
              Send report
            </TapeDeckButton>
            <TapeDeckButton size="sm" onClick={() => setReporting(false)}>
              Cancel
            </TapeDeckButton>
          </div>
        </form>
      )}
    </div>
  );
};
