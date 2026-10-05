import React, { useId } from 'react';
import { Plus } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';

export interface ProfileSectionProps {
  title: string;
  hint: string;
  /** Label for the Add key; omit `onAdd` to hide it. */
  addLabel?: string;
  onAdd?: () => void;
  children: React.ReactNode;
}

/** A titled block on the Profiles page: header, one-line hint, an Add key, then the section body. */
export const ProfileSection: React.FC<ProfileSectionProps> = ({ title, hint, addLabel = 'Add', onAdd, children }) => {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <header className="flex items-start justify-between gap-3 border-b border-[var(--border-subtle)] pb-2">
        <div className="min-w-0">
          <h3 id={headingId} className="text-sm font-bold uppercase tracking-wider text-white">
            {title}
          </h3>
          <p className="mt-0.5 text-[11px] font-mono text-[var(--text-muted)]">{hint}</p>
        </div>
        {onAdd && (
          <TapeDeckButton size="sm" variant="amber" onClick={onAdd} icon={<Plus className="h-3.5 w-3.5" />} aria-label={`${addLabel} ${title}`}>
            {addLabel}
          </TapeDeckButton>
        )}
      </header>
      {children}
    </section>
  );
};

export const EmptyNote: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <p className="py-2 text-xs font-mono text-neutral-500">{children}</p>
);

export const Badge: React.FC<{ children: React.ReactNode; tone?: 'amber' | 'muted' | 'error' }> = ({ children, tone = 'muted' }) => {
  const toneClass = {
    amber: 'border-[var(--accent-amber)]/50 text-[var(--accent-amber)]',
    muted: 'border-[var(--border-default)] text-[var(--text-secondary)]',
    error: 'border-[var(--status-error)]/60 text-[var(--status-error)]',
  }[tone];
  return <span className={`inline-block rounded-[2px] border px-1.5 py-px text-[10px] font-mono uppercase tracking-wider ${toneClass}`}>{children}</span>;
};
