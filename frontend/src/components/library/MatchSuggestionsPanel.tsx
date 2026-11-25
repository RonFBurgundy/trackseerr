import React, { useState } from 'react';
import { ChevronDown, ExternalLink, FileCheck, Loader2 } from 'lucide-react';
import type { MatchCandidate } from '@/types/models';
import { useMatchSuggestions } from '@/hooks/useMatchSuggestions';
import { SURE_CONFIDENCE } from '@/hooks/useStoredSuggestions';
import { errorMessage } from '@/services/apiClient';
import { TapeDeckButton } from '@/components/ui';
import { CHIP_BASE, CHIP_RISKY, CHIP_SAFE } from './libraryChips';

export interface MatchSuggestionsPanelProps {
  albumId: string;
  albumTitle: string;
  onBack: () => void;
  /** Called after a match is applied, so the caller can refresh its list and open the file review. */
  onApplied: (albumId: string, albumTitle: string) => void;
  onSubmitToMusicBrainz: (albumId: string) => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

function releaseKind(c: MatchCandidate): string {
  const parts = [c.primary_type, ...c.secondary_types].filter((p): p is string => Boolean(p));
  const year = c.first_release_date ? c.first_release_date.slice(0, 4) : '';
  const label = parts.join(' + ');
  return [label, year].filter(Boolean).join(', ');
}

const CandidateRow: React.FC<{
  candidate: MatchCandidate;
  albumTrackCount: number;
  disabled: boolean;
  isApplying: boolean;
  onUse: () => void;
}> = ({ candidate, albumTrackCount, disabled, isApplying, onUse }) => {
  const [open, setOpen] = useState<boolean>(false);
  const artist = candidate.is_various ? 'Various Artists' : candidate.artist_credit || candidate.target_artist;
  const kind = releaseKind(candidate);
  const percent = Math.round(candidate.confidence);
  return (
    <div className="p-2.5 flex flex-col gap-1 hover:bg-[#141414]">
      <span className="text-white font-medium truncate">{candidate.title || 'Untitled'}</span>
      <span className="text-xs text-neutral-400 truncate">
        {artist}
        {kind ? ` · ${kind}` : ''}
      </span>
      <div className="tab-strip mt-0.5 overflow-x-auto">
        <span className={`${CHIP_BASE} ${percent >= SURE_CONFIDENCE ? CHIP_SAFE : CHIP_RISKY}`}>
          {percent}% sure
        </span>
        <span className={`${CHIP_BASE} ${candidate.mapped < albumTrackCount ? CHIP_RISKY : CHIP_SAFE}`}>
          {candidate.mapped} of your {albumTrackCount} {albumTrackCount === 1 ? 'song' : 'songs'} found
        </span>
        <span className={`${CHIP_BASE} ${CHIP_SAFE}`}>
          {candidate.track_count} tracks on this release
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5 mt-0.5">
        <TapeDeckButton size="sm" variant="amber" disabled={disabled} onClick={onUse}>
          {isApplying ? <Loader2 className="h-3 w-3 animate-spin" /> : 'Use this match'}
        </TapeDeckButton>
        <TapeDeckButton
          size="sm"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          icon={<ChevronDown className={`h-3.5 w-3.5 transition-transform ${open ? 'rotate-180' : ''}`} />}
        >
          {open ? 'Hide details' : 'Show details'}
        </TapeDeckButton>
      </div>
      {open && (
        <div className="border-t border-[#222222] pt-2 font-mono text-[11px] flex flex-col gap-2">
          {candidate.reasons.length > 0 && (
            <ul className="text-neutral-400 list-disc pl-4 space-y-0.5">
              {candidate.reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          )}
          <ul className="flex flex-col gap-0.5">
            {candidate.track_map.map((m) => (
              <li key={m.track_id} className="flex gap-2">
                <span className="text-neutral-300 truncate min-w-0 flex-1">{m.title}</span>
                <span className="text-neutral-500 shrink-0">&rarr;</span>
                <span className={`truncate min-w-0 flex-1 ${m.release_track_number != null ? 'text-neutral-300' : 'text-neutral-500'}`}>
                  {m.release_track_number != null
                    ? `${m.release_track_number}. ${m.release_track_title ?? ''}`
                    : 'No matching track'}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};

export const MatchSuggestionsPanel: React.FC<MatchSuggestionsPanelProps> = ({
  albumId,
  albumTitle,
  onBack,
  onApplied,
  onSubmitToMusicBrainz,
  onToast,
}) => {
  const { data, isLoading, error, isApplying, apply, reload } = useMatchSuggestions(albumId);
  const [applyingId, setApplyingId] = useState<string | null>(null);

  const handleUse = async (candidate: MatchCandidate): Promise<void> => {
    setApplyingId(candidate.id);
    try {
      const res = await apply(candidate);
      if (res.conflict) {
        onToast('Matched, but some files need a decision first. Open Duplicates to choose.', 'ok');
      } else {
        onToast(`Matched "${albumTitle}" to MusicBrainz`, 'ok');
      }
      onApplied(res.album_id || albumId, albumTitle);
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Could not apply this match'), 'error');
    } finally {
      setApplyingId(null);
    }
  };

  const albumTrackCount = data?.album.track_count ?? 0;

  let body: React.ReactNode;
  if (isLoading) {
    body = (
      <div className="flex flex-col items-center justify-center gap-2 py-14 text-xs font-mono uppercase tracking-widest text-[var(--text-secondary)]">
        <Loader2 className="h-6 w-6 animate-spin text-[var(--accent-amber)]" />
        <span>Looking on MusicBrainz&hellip;</span>
      </div>
    );
  } else if (error) {
    body = (
      <div
        role="alert"
        className="p-3 rounded-[3px] border border-[var(--status-error)]/50 bg-[var(--status-error)]/10 text-xs font-mono text-[var(--status-error)] flex flex-wrap items-center justify-between gap-2"
      >
        <span>{error}</span>
        <TapeDeckButton size="sm" onClick={reload}>
          Try again
        </TapeDeckButton>
      </div>
    );
  } else if (data?.unavailable) {
    body = (
      <div
        role="alert"
        className="p-3 rounded-[3px] border border-[var(--status-error)]/50 bg-[var(--status-error)]/10 text-xs font-mono text-[var(--status-error)] flex flex-wrap items-center justify-between gap-2"
      >
        <span>MusicBrainz is not answering, try again later.</span>
        <TapeDeckButton size="sm" onClick={reload}>
          Try again
        </TapeDeckButton>
      </div>
    );
  } else if (data && data.candidates.length === 0) {
    body = (
      <div className="py-12 text-center space-y-2">
        <FileCheck className="h-8 w-8 text-[var(--accent-amber)] mx-auto opacity-70" />
        <p className="text-sm font-medium text-white">No match found on MusicBrainz</p>
        <div className="pt-1">
          <TapeDeckButton
            size="sm"
            onClick={() => onSubmitToMusicBrainz(albumId)}
            icon={<ExternalLink className="h-3.5 w-3.5" />}
            title="Opens MusicBrainz with this album prefilled"
          >
            Submit to MusicBrainz
          </TapeDeckButton>
        </div>
      </div>
    );
  } else {
    body = (
      <div
        tabIndex={0}
        role="region"
        aria-label="Possible matches (scrollable)"
        className="overflow-y-auto border border-[#222222] rounded-[4px] bg-[#0d0d0d] divide-y divide-[#1c1c1c] flex-1 min-h-[16rem] sm:flex-none sm:max-h-[min(450px,55vh)]"
      >
        {(data?.candidates ?? []).map((c) => (
          <CandidateRow
            key={c.id}
            candidate={c}
            albumTrackCount={albumTrackCount}
            disabled={isApplying}
            isApplying={isApplying && applyingId === c.id}
            onUse={() => void handleUse(c)}
          />
        ))}
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex items-center justify-between gap-2 shrink-0">
        <div className="min-w-0">
          <p className="text-xs font-mono text-neutral-400">Possible matches for</p>
          <p className="text-white font-medium truncate">{albumTitle}</p>
        </div>
        <TapeDeckButton size="sm" onClick={onBack}>
          Back to list
        </TapeDeckButton>
      </div>
      {body}
    </div>
  );
};
