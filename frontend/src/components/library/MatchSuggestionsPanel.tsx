import React, { useState } from 'react';
import { ChevronDown, ExternalLink, Loader2 } from 'lucide-react';
import type { MatchCandidate } from '@/types/models';
import { useMatchSuggestions } from '@/hooks/useMatchSuggestions';
import { errorMessage } from '@/services/apiClient';
import { TapeDeckButton } from '@/components/ui';

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
  disabled: boolean;
  isApplying: boolean;
  onUse: () => void;
}> = ({ candidate, disabled, isApplying, onUse }) => {
  const [open, setOpen] = useState<boolean>(false);
  const artist = candidate.is_various ? 'Various Artists' : candidate.artist_credit || candidate.target_artist;
  const kind = releaseKind(candidate);
  const percent = Math.round(candidate.confidence * 100);
  return (
    <div className="border border-[#2a2a2a] bg-[#121212] rounded-[4px] p-2.5 flex flex-col gap-2">
      <div className="flex items-start justify-between gap-2 flex-wrap">
        <div className="min-w-0 flex flex-col">
          <span className="text-white font-medium truncate">{candidate.title || 'Untitled'}</span>
          <span className="text-xs text-neutral-400 truncate">{artist}</span>
          {kind ? <span className="text-xs text-neutral-500 font-mono">{kind}</span> : null}
        </div>
        <div className="flex flex-col items-end gap-0.5 shrink-0">
          <span className="text-xs font-mono text-[var(--accent-amber)]">{percent}% sure</span>
          <span className="text-xs font-mono text-neutral-400">
            {candidate.mapped} of {candidate.track_count} files found
          </span>
        </div>
      </div>
      {candidate.reasons.length > 0 && (
        <ul className="text-xs text-neutral-400 list-disc pl-4">
          {candidate.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
      <div className="flex items-center gap-1.5 flex-wrap">
        <TapeDeckButton size="sm" variant="amber" disabled={disabled} onClick={onUse}>
          {isApplying ? <Loader2 className="h-3 w-3 animate-spin" /> : 'Use this match'}
        </TapeDeckButton>
        <TapeDeckButton
          size="sm"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          icon={<ChevronDown className={`h-3.5 w-3.5 transition-transform ${open ? 'rotate-180' : ''}`} />}
        >
          {open ? 'Hide tracks' : 'Show tracks'}
        </TapeDeckButton>
      </div>
      {open && (
        <ul className="text-xs font-mono flex flex-col gap-0.5 border-t border-[#222222] pt-2">
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

  let body: React.ReactNode;
  if (isLoading) {
    body = (
      <div className="flex items-center gap-2 text-xs text-neutral-400 py-4">
        <Loader2 className="h-4 w-4 animate-spin" /> Looking on MusicBrainz&hellip;
      </div>
    );
  } else if (error) {
    body = (
      <div className="flex flex-col gap-2 py-2">
        <p className="text-xs text-[var(--status-error)]">{error}</p>
        <div>
          <TapeDeckButton size="sm" onClick={reload}>
            Try again
          </TapeDeckButton>
        </div>
      </div>
    );
  } else if (data?.unavailable) {
    body = (
      <div className="flex flex-col gap-2 py-2">
        <p className="text-xs text-neutral-300">MusicBrainz is not answering, try again later.</p>
        <div>
          <TapeDeckButton size="sm" onClick={reload}>
            Try again
          </TapeDeckButton>
        </div>
      </div>
    );
  } else if (data && data.candidates.length === 0) {
    body = (
      <div className="flex flex-col gap-2 py-2">
        <p className="text-xs text-neutral-300">No match found on MusicBrainz.</p>
        <div>
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
      <div className="flex flex-col gap-2">
        {(data?.candidates ?? []).map((c) => (
          <CandidateRow
            key={c.id}
            candidate={c}
            disabled={isApplying}
            isApplying={isApplying && applyingId === c.id}
            onUse={() => void handleUse(c)}
          />
        ))}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 min-h-0">
      <div className="flex items-center justify-between gap-2 shrink-0">
        <div className="min-w-0">
          <p className="text-xs text-neutral-500">Possible matches for</p>
          <p className="text-white font-medium truncate">{albumTitle}</p>
        </div>
        <TapeDeckButton size="sm" onClick={onBack}>
          Back to list
        </TapeDeckButton>
      </div>
      <div className="min-h-0 overflow-y-auto max-h-[55dvh]">{body}</div>
    </div>
  );
};
