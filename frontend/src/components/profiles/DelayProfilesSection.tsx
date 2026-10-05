import React, { useState } from 'react';
import { Pencil, Pin, Trash2 } from 'lucide-react';
import { ConfirmDangerButton, MachinedCard, SortableList, SortControls, TapeDeckButton } from '@/components/ui';
import type { DelayProfile } from '@/types/delayProfiles';
import { useDelayProfiles, usePendingReleases } from '@/hooks/useDelayProfiles';
import { DelayProfileEditorModal, type DelayProfileTarget } from './DelayProfileEditorModal';
import { PendingReleasesList } from './PendingReleasesList';
import { Badge, EmptyNote, ProfileSection } from './ProfileSection';

function describe(p: DelayProfile): string {
  const d = p.delays;
  return `prefers ${p.preferred_protocol} | delay usenet ${d.usenet}m, torrent ${d.torrent}m, soulseek ${d.soulseek}m`;
}

const Summary: React.FC<{ p: DelayProfile }> = ({ p }) => (
  <div className="min-w-0">
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="break-words text-sm font-bold text-white">{p.name}</span>
      {p.is_default && <Badge>Default, last</Badge>}
      {p.tags.map((t) => (
        <Badge key={t} tone="amber">
          {t}
        </Badge>
      ))}
    </div>
    <p className="mt-1 text-[11px] font-mono text-neutral-400">{describe(p)}</p>
    <p className="text-[11px] font-mono text-neutral-500">
      {p.bypass_if_highest_quality ? 'Bypass at highest quality' : 'No quality bypass'}
      {p.bypass_if_above_score !== null ? ` | bypass above score ${p.bypass_if_above_score}` : ''}
    </p>
  </div>
);

export interface DelayProfilesSectionProps {
  enabled: boolean;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Ordered delay profiles (first match wins; the default is pinned last) plus the queue of held-back releases. */
export const DelayProfilesSection: React.FC<DelayProfilesSectionProps> = ({ enabled, onToast }) => {
  const { profiles, loading, save, remove, reorder } = useDelayProfiles(enabled, onToast);
  const pending = usePendingReleases(enabled, onToast);
  const [editing, setEditing] = useState<DelayProfileTarget | null>(null);
  const movable = profiles.filter((p) => !p.is_default);
  const pinned = profiles.filter((p) => p.is_default);

  return (
    <ProfileSection
      title="Delay Profiles"
      hint="Hold a release back for a while so a better one can show up. The first matching profile applies; the default is always last."
      onAdd={() => setEditing('new')}
    >
      {loading && profiles.length === 0 ? (
        <EmptyNote>Loading delay profiles...</EmptyNote>
      ) : (
        <div className="space-y-2">
          <SortableList
            ariaLabel="Delay profiles in priority order"
            items={movable}
            getKey={(p) => String(p.id)}
            onReorder={(next) => void reorder(next.map((p) => p.id))}
            className="space-y-2"
            renderItem={(p, state) => (
              <MachinedCard className="flex items-start justify-between gap-2 p-2.5">
                <Summary p={p} />
                <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
                  <SortControls state={state} label={p.name} />
                  <TapeDeckButton size="sm" aria-label={`Edit ${p.name}`} onClick={() => setEditing(p)} icon={<Pencil className="h-3.5 w-3.5" />} />
                  <ConfirmDangerButton onConfirm={() => void remove(p.id)} icon={<Trash2 className="h-3 w-3" />} ariaLabel={`Delete ${p.name}`} confirmLabel="Confirm Delete" />
                </div>
              </MachinedCard>
            )}
          />
          {pinned.map((p) => (
            <MachinedCard key={p.id} className="flex items-start justify-between gap-2 p-2.5">
              <Summary p={p} />
              <div className="flex shrink-0 items-center gap-1.5">
                <span className="inline-flex h-9 w-7 items-center justify-center text-neutral-600" title="Pinned last" aria-hidden="true">
                  <Pin className="h-3.5 w-3.5" />
                </span>
                <TapeDeckButton size="sm" aria-label={`Edit ${p.name}`} onClick={() => setEditing(p)} icon={<Pencil className="h-3.5 w-3.5" />} />
              </div>
            </MachinedCard>
          ))}
        </div>
      )}
      <PendingReleasesList manager={pending} />
      <DelayProfileEditorModal target={editing} onClose={() => setEditing(null)} onSave={save} />
    </ProfileSection>
  );
};
