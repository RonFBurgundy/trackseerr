import React, { useState } from 'react';
import { ChevronDown, Loader2 } from 'lucide-react';
import type { DuplicateChoice, DuplicateFileRef, DuplicateResolveChoice, DuplicateResolveResponse } from '@/types/models';
import { useDuplicateChoices } from '@/hooks/useDuplicateChoices';
import { errorMessage } from '@/services/apiClient';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import { formatBytes } from '@/components/lists';

export interface DuplicatesModalProps {
  isOpen: boolean;
  onClose: () => void;
  /** Reports how many choices are still open after a change, so the menu label can follow. */
  onCountChange?: (count: number) => void;
  /** Called after a choice is applied, so the library can refresh. */
  onResolved?: () => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

function folderOf(path: string): string {
  const i = path.lastIndexOf('/');
  return i > 0 ? path.slice(0, i) : path;
}

function describeFile(f: DuplicateFileRef): string[] {
  const lines: string[] = [];
  const quality = [f.quality_name, f.codec ? f.codec.toUpperCase() : null].filter(Boolean).join(' · ');
  if (quality) lines.push(quality);
  const depth = [
    f.bits_per_sample ? `${f.bits_per_sample}-bit` : null,
    f.sample_rate ? `${parseFloat((f.sample_rate / 1000).toFixed(1))} kHz` : null,
  ]
    .filter(Boolean)
    .join(' / ');
  if (depth) lines.push(depth);
  if (f.bitrate) lines.push(`${Math.round(f.bitrate / 1000)} kbps`);
  if (f.size_bytes) lines.push(formatBytes(f.size_bytes));
  return lines;
}

const FileSide: React.FC<{ label: string; file: DuplicateFileRef }> = ({ label, file }) => (
  <div className="min-w-0 flex-1 flex flex-col gap-0.5">
    <span className="text-[10px] uppercase tracking-wider text-neutral-500">{label}</span>
    <span className="text-xs text-neutral-200 font-mono">{describeFile(file).join(' · ') || 'No details'}</span>
    <span className="text-[11px] text-neutral-500 font-mono truncate" title={file.file_path}>
      {folderOf(file.file_path)}
    </span>
  </div>
);

/** Plain-English line for what a choice did, using the kept and recycled file counts the server reports. */
function resolvedMessage(choice: DuplicateResolveChoice, item: DuplicateChoice, res: DuplicateResolveResponse): string {
  if (choice === 'keep_both') return 'Kept both albums as they are';
  if (res.recycled_file_ids.length === 0) {
    return res.merged ? `Merged "${item.source_title}" into "${item.holder_title}"` : 'Done';
  }
  const n = res.recycled_file_ids.length;
  const keptId = res.kept_file_ids[0];
  const kept = item.pairs
    .flatMap((p) => [p.holder_file, p.source_file])
    .find((f) => f.id === keptId);
  const keptLabel = kept
    ? [kept.codec ? kept.codec.toUpperCase() : null, kept.bits_per_sample && kept.sample_rate ? `${kept.bits_per_sample}/${parseFloat((kept.sample_rate / 1000).toFixed(1))}` : null]
        .filter(Boolean)
        .join(' ')
    : '';
  const head = keptLabel ? `Kept ${keptLabel}` : 'Kept the copy you chose';
  return `${head}, moved the other ${n === 1 ? 'copy' : `${n} copies`} to the recycle bin`;
}

function qualitySignature(f: DuplicateFileRef): string {
  return `${f.quality_name ?? ''}|${f.codec ?? ''}|${f.bits_per_sample ?? ''}|${f.sample_rate ?? ''}`;
}

export const DuplicatesModal: React.FC<DuplicatesModalProps> = ({ isOpen, onClose, onCountChange, onResolved, onToast }) => {
  const { items, isLoading, error, resolvingId, resolveError, resolve, reload } = useDuplicateChoices(isOpen);
  const [expandedIds, setExpandedIds] = useState<Set<number>>(() => new Set());

  const toggleExpanded = (id: number): void => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  React.useEffect(() => {
    if (isOpen && !isLoading && !error) onCountChange?.(items.length);
  }, [isOpen, isLoading, error, items.length, onCountChange]);

  const handle = async (item: DuplicateChoice, choice: DuplicateResolveChoice): Promise<void> => {
    try {
      const res = await resolve(item.id, choice);
      onToast(resolvedMessage(choice, item, res), 'ok');
      onResolved?.();
    } catch (err: unknown) {
      // The server's own message (for example a 409) is shown inline below; toast it too so it is not missed.
      onToast(errorMessage(err, 'Could not resolve this duplicate'), 'error');
    }
  };

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="Duplicates"
      subtitle="Albums that turned out to be the same release"
      maxWidth="sm:max-w-4xl"
    >
      <div className="flex flex-col gap-3 min-h-0 h-full">
        <p className="text-xs text-neutral-400 font-mono shrink-0">
          These albums were merged by MusicBrainz match, but both had a file for the same track. Pick which copy to keep.
          The copy you don&apos;t keep goes to the recycle bin.
        </p>
        {resolveError && <p className="text-xs text-[var(--status-error)] shrink-0">{resolveError}</p>}
        <div className="min-w-0 min-h-0 overflow-y-auto flex-1 sm:flex-none sm:max-h-[60dvh] flex flex-col gap-3">
          {isLoading && (
            <div className="flex items-center gap-2 text-xs text-neutral-400 py-4">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading&hellip;
            </div>
          )}
          {error && (
            <div className="flex flex-col gap-2 py-2">
              <p className="text-xs text-[var(--status-error)]">{error}</p>
              <div>
                <TapeDeckButton size="sm" onClick={reload}>
                  Try again
                </TapeDeckButton>
              </div>
            </div>
          )}
          {!isLoading && !error && items.length === 0 && (
            <p className="text-xs text-neutral-400 py-4">No duplicates to sort out.</p>
          )}
          {items.map((item) => {
            const busy = resolvingId === item.id;
            const ambiguous = item.kind === 'ambiguous';
            const isExpanded = expandedIds.has(item.id);
            const firstPair = item.pairs[0];
            const sameQuality =
              firstPair !== undefined &&
              item.pairs.every(
                (p) =>
                  qualitySignature(p.holder_file) === qualitySignature(firstPair.holder_file) &&
                  qualitySignature(p.source_file) === qualitySignature(firstPair.source_file)
              );
            return (
              <div key={item.id} className="border border-[#2a2a2a] bg-[#121212] rounded-[4px] p-2.5 flex flex-col gap-2 min-w-0">
                <div className="flex flex-col min-w-0">
                  <span className="text-white font-medium block truncate">
                    {item.source_title} &rarr; {item.holder_title}
                  </span>
                  <span className="text-xs text-neutral-400 block truncate">
                    {item.source_artist === item.holder_artist
                      ? item.holder_artist
                      : `${item.source_artist} / ${item.holder_artist}`}
                  </span>
                </div>
                <div className="flex items-center gap-1.5 flex-wrap">
                  {ambiguous ? (
                    <>
                      <TapeDeckButton
                        size="sm"
                        variant="amber"
                        disabled={busy}
                        onClick={() => void handle(item, 'keep_existing')}
                      >
                        {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : 'Merge into this album'}
                      </TapeDeckButton>
                      <TapeDeckButton size="sm" disabled={busy} onClick={() => void handle(item, 'keep_both')}>
                        Keep both
                      </TapeDeckButton>
                    </>
                  ) : (
                    <>
                      <TapeDeckButton size="sm" disabled={busy} onClick={() => void handle(item, 'keep_existing')}>
                        Keep existing
                      </TapeDeckButton>
                      <TapeDeckButton size="sm" disabled={busy} onClick={() => void handle(item, 'keep_incoming')}>
                        Keep incoming
                      </TapeDeckButton>
                      <TapeDeckButton
                        size="sm"
                        variant="amber"
                        disabled={busy}
                        onClick={() => void handle(item, 'prefer_quality')}
                      >
                        {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : 'Keep better quality'}
                      </TapeDeckButton>
                      <TapeDeckButton size="sm" disabled={busy} onClick={() => void handle(item, 'keep_both')}>
                        Keep both
                      </TapeDeckButton>
                    </>
                  )}
                </div>
                {ambiguous && (
                  <p className="text-xs text-neutral-400">
                    This album could belong to more than one existing album. Choose the one it should be merged into.
                  </p>
                )}
                {item.pairs.length > 0 && (
                  <div className="flex flex-col gap-1.5 border-t border-[#222222] pt-2">
                    {!isExpanded && (
                      <div className="flex flex-col gap-0.5 text-xs min-w-0">
                        <span className="text-neutral-300">
                          {item.pairs.length} {item.pairs.length === 1 ? 'track' : 'tracks'} in both
                        </span>
                        {sameQuality && firstPair && (
                          <>
                            <span className="text-neutral-400 font-mono block truncate">
                              Existing: {describeFile(firstPair.holder_file).join(' · ') || 'No details'}
                            </span>
                            <span className="text-neutral-400 font-mono block truncate">
                              Incoming: {describeFile(firstPair.source_file).join(' · ') || 'No details'}
                            </span>
                          </>
                        )}
                      </div>
                    )}
                    <div>
                      <TapeDeckButton
                        size="sm"
                        onClick={() => toggleExpanded(item.id)}
                        aria-expanded={isExpanded}
                        icon={<ChevronDown className={`h-3.5 w-3.5 transition-transform ${isExpanded ? 'rotate-180' : ''}`} />}
                      >
                        {isExpanded ? 'Hide tracks' : 'Show tracks'}
                      </TapeDeckButton>
                    </div>
                    {isExpanded &&
                      item.pairs.map((pair) => (
                        <div
                          key={`${pair.source_file.id}-${pair.holder_file.id}`}
                          className="flex flex-col gap-1 border-t border-[#222222] pt-2"
                        >
                          <span className="text-xs text-neutral-300 truncate">{pair.holder_track.title}</span>
                          <div className="flex flex-col sm:flex-row gap-2 sm:gap-4">
                            <FileSide label="Existing" file={pair.holder_file} />
                            <FileSide label="Incoming" file={pair.source_file} />
                          </div>
                        </div>
                      ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </ObsidianModal>
  );
};
