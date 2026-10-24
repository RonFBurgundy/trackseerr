import React, { useState } from 'react';
import { formatDateTime } from '@/components/lists';
import { FormField, TapeDeckButton } from '@/components/ui';
import type { SlskdChallenge } from '@/types/slskdChallenges';

export interface ReviewSlskdChallengesProps {
  items: readonly SlskdChallenge[];
  busyId: string | null;
  onReply: (id: string, text: string) => Promise<void>;
  onDismiss: (id: string) => Promise<void>;
}

interface ChallengeCardProps {
  item: SlskdChallenge;
  busy: boolean;
  onReply: (id: string, text: string) => Promise<void>;
  onDismiss: (id: string) => Promise<void>;
}

const ChallengeCard: React.FC<ChallengeCardProps> = ({ item, busy, onReply, onDismiss }) => {
  const [text, setText] = useState<string>('');
  const send = async (): Promise<void> => {
    await onReply(item.id, text.trim());
    setText('');
  };
  return (
    <div className="w-full min-w-0 space-y-2 p-3 border-b border-[#222222] last:border-b-0">
      <p className="text-xs font-mono text-white break-words">
        <span className="font-bold">{item.username}</span>
        {item.download_title && <span className="text-[var(--text-secondary)]"> · {item.download_title}</span>}
      </p>
      <p className="whitespace-pre-wrap break-words select-text text-sm font-mono text-[var(--text-primary)]">{item.message}</p>
      <p className="text-[11px] font-mono text-[var(--text-muted)]">
        Received {formatDateTime(item.received_at)}
        {item.expires_at ? ` · Expires ${formatDateTime(item.expires_at)}` : ''}
      </p>
      <FormField label="Your reply" name="Your reply" htmlFor={`challenge-reply-${item.id}`}>
        <input
          id={`challenge-reply-${item.id}`}
          name="Your reply"
          type="text"
          value={text}
          maxLength={200}
          autoComplete="off"
          disabled={busy}
          onChange={(e) => setText(e.target.value)}
          className="w-full bg-[var(--bg-canvas)] border border-[var(--border-default)] rounded-[3px] px-3 py-2 text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-amber)] min-h-[38px] disabled:opacity-50"
        />
      </FormField>
      <div className="flex flex-col sm:flex-row gap-2">
        <TapeDeckButton variant="amber" size="sm" disabled={busy || text.trim() === ''} onClick={() => void send()}>
          Send reply
        </TapeDeckButton>
        <TapeDeckButton size="sm" disabled={busy} onClick={() => void onDismiss(item.id)}>
          Dismiss
        </TapeDeckButton>
      </div>
    </div>
  );
};

export const ReviewSlskdChallenges: React.FC<ReviewSlskdChallengesProps> = React.memo(
  ({ items, busyId, onReply, onDismiss }) => (
    <section aria-label="Sharers asking for a reply" className="space-y-2">
      <h5 className="text-[11px] font-bold uppercase tracking-widest font-mono text-[var(--text-secondary)]">
        Sharers asking for a reply
        <span className="ml-2 font-normal text-[var(--text-muted)]">{items.length}</span>
      </h5>
      <p className="text-[11px] font-mono text-[var(--text-muted)]">
        Trackseerr already tried another source. Reply to unlock this sharer for later downloads.
      </p>
      <div className="rounded-[4px] border border-[#222222] overflow-hidden">
        {items.map((item) => (
          <ChallengeCard key={item.id} item={item} busy={busyId === item.id} onReply={onReply} onDismiss={onDismiss} />
        ))}
      </div>
    </section>
  )
);
ReviewSlskdChallenges.displayName = 'ReviewSlskdChallenges';
