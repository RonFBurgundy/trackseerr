import React from 'react';
import type { ListenForwardStatus, UserListen } from '@/types/models';
import { MachinedCard } from '@/components/ui';

const STATUS_STYLE: Record<ListenForwardStatus, string> = {
  sent: 'bg-[#22c55e]',
  pending: 'bg-[#e5a00d]',
  failed: 'bg-[#ef4444]',
  skipped: 'bg-[#666666]',
};

const StatusDot: React.FC<{ label: string; status: ListenForwardStatus }> = ({ label, status }) => (
  <span
    title={`${label}: ${status}`}
    aria-label={`${label}: ${status}`}
    className="inline-flex items-center gap-1 text-[10px] font-mono uppercase text-neutral-400"
  >
    <span className={`h-2 w-2 rounded-full ${STATUS_STYLE[status]}`} />
    {label}
  </span>
);

export interface RecentListensProps {
  listens: UserListen[];
}

export const RecentListens: React.FC<RecentListensProps> = ({ listens }) => (
  <MachinedCard className="p-3 sm:p-6">
    <h3 className="text-sm font-bold uppercase tracking-wider text-white mb-4">Recent Listens</h3>
    {listens.length === 0 ? (
      <p className="text-sm text-neutral-500 font-mono">No listens captured yet.</p>
    ) : (
      <ul className="divide-y divide-[#222222]">
        {listens.map((l) => (
          <li key={l.id} className="py-2.5 flex flex-col sm:flex-row sm:items-center justify-between gap-1.5">
            <div className="min-w-0">
              <div className="text-sm text-white truncate">{l.title}</div>
              <div className="text-xs text-neutral-400 truncate">
                {l.artist}
                {l.album ? ` · ${l.album}` : ''}
              </div>
            </div>
            <div className="flex items-center gap-3 shrink-0">
              <StatusDot label="LFM" status={l.lastfm_status} />
              <StatusDot label="LB" status={l.listenbrainz_status} />
              <span className="text-[10px] font-mono text-neutral-500">
                {new Date(l.played_at).toLocaleString()}
              </span>
            </div>
          </li>
        ))}
      </ul>
    )}
  </MachinedCard>
);
