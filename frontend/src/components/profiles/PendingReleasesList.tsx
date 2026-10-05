import React from 'react';
import { Download, RefreshCw, Trash2 } from 'lucide-react';
import { ConfirmDangerButton, MachinedCard, TapeDeckButton } from '@/components/ui';
import type { UsePendingReleasesReturn } from '@/hooks/useDelayProfiles';
import { EmptyNote } from './ProfileSection';

function releasesIn(iso: string): string {
  const ms = Date.parse(iso) - Date.now();
  if (Number.isNaN(ms)) return 'unknown';
  if (ms <= 0) return 'due now';
  const min = Math.ceil(ms / 60000);
  return min >= 120 ? `in ${Math.round(min / 60)} h` : `in ${min} min`;
}

/** Releases currently held back by a delay profile, with drop and grab-now. */
export const PendingReleasesList: React.FC<{ manager: UsePendingReleasesReturn }> = ({ manager }) => {
  const { pending, loading, drop, grabNow, reload } = manager;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-xs font-bold uppercase tracking-wider text-neutral-300">Pending releases ({pending.length})</h4>
        <TapeDeckButton size="sm" aria-label="Refresh pending releases" onClick={() => void reload()} disabled={loading} icon={<RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />} />
      </div>
      {pending.length === 0 ? (
        <EmptyNote>Nothing is waiting on a delay.</EmptyNote>
      ) : (
        <ul className="space-y-2">
          {pending.map((p) => (
            <li key={p.id}>
              <MachinedCard className="flex items-center justify-between gap-2 p-2.5">
                <div className="min-w-0">
                  <p className="truncate text-[13px] text-white" title={p.title}>
                    {p.title}
                  </p>
                  <p className="truncate text-[11px] font-mono text-neutral-400">
                    {[p.artist_name, p.protocol, p.quality, p.format_score !== null ? `score ${p.format_score}` : null].filter(Boolean).join(' | ')}
                  </p>
                  <p className="text-[11px] font-mono text-neutral-500">
                    Grabs {releasesIn(p.release_at)}
                    {p.reason ? ` - ${p.reason}` : ''}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  <TapeDeckButton size="sm" aria-label={`Grab ${p.title} now`} onClick={() => void grabNow(p.id)} icon={<Download className="h-3.5 w-3.5" />}>
                    Grab
                  </TapeDeckButton>
                  <ConfirmDangerButton onConfirm={() => void drop(p.id)} icon={<Trash2 className="h-3 w-3" />} ariaLabel={`Drop ${p.title}`} confirmLabel="Drop" />
                </div>
              </MachinedCard>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};
