import React, { useState } from 'react';
import { Pencil, Trash2 } from 'lucide-react';
import { ConfirmDangerButton, MachinedCard, TactileSwitch, TapeDeckButton, isRegexTerm } from '@/components/ui';
import type { IndexerItem } from '@/types/models';
import type { QualityProfile } from '@/types/qualityProfiles';
import { SEEDED_RELEASE_PROFILE_NAME } from '@/types/releaseProfiles';
import { useReleaseProfiles } from '@/hooks/useReleaseProfiles';
import { Badge, EmptyNote, ProfileSection } from './ProfileSection';
import { ReleaseProfileEditorModal, type ReleaseProfileTarget } from './ReleaseProfileEditorModal';

export interface ReleaseProfilesSectionProps {
  enabled: boolean;
  indexers: readonly IndexerItem[];
  qualityProfiles: readonly QualityProfile[];
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

const Terms: React.FC<{ label: string; terms: readonly string[] }> = ({ label, terms }) =>
  terms.length === 0 ? null : (
    <p className="line-clamp-2 break-words text-[11px] font-mono text-neutral-400">
      <span className="text-neutral-500">{label}: </span>
      {terms.map((t, i) => (
        <span key={t} className={isRegexTerm(t) ? 'text-[var(--accent-amber)]' : ''}>
          {i > 0 ? ', ' : ''}
          {t}
        </span>
      ))}
    </p>
  );

/** Term-based release profiles: required / ignored terms with optional indexer and quality-profile scope. */
export const ReleaseProfilesSection: React.FC<ReleaseProfilesSectionProps> = ({ enabled, indexers, qualityProfiles, onToast }) => {
  const { profiles, loading, save, remove, toggleEnabled } = useReleaseProfiles(enabled, onToast);
  const [editing, setEditing] = useState<ReleaseProfileTarget | null>(null);

  return (
    <ProfileSection
      title="Release Profiles"
      hint="Reject or require releases by words or /regex/ before they are scored."
      onAdd={() => setEditing('new')}
    >
      {loading && profiles.length === 0 ? (
        <EmptyNote>Loading release profiles...</EmptyNote>
      ) : profiles.length === 0 ? (
        <EmptyNote>No release profiles defined.</EmptyNote>
      ) : (
        <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
          {profiles.map((p) => (
            <MachinedCard key={p.id} className="space-y-1.5 p-2.5 sm:p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="break-words text-sm font-bold text-white">{p.name}</span>
                    {p.name === SEEDED_RELEASE_PROFILE_NAME && <Badge tone="amber">Suggested default</Badge>}
                    {!p.enabled && <Badge>Disabled</Badge>}
                  </div>
                  <p className="mt-0.5 text-[11px] font-mono text-neutral-500">
                    {p.indexer_ids.length === 0 ? 'All indexers' : `${p.indexer_ids.length} indexer(s)`} |{' '}
                    {p.quality_profile_ids.length === 0 ? 'all quality profiles' : `${p.quality_profile_ids.length} quality profile(s)`}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  <TactileSwitch ariaLabel={`Enable ${p.name}`} checked={p.enabled} onChange={(v) => void toggleEnabled(p, v)} />
                  <TapeDeckButton size="sm" aria-label={`Edit ${p.name}`} onClick={() => setEditing(p)} icon={<Pencil className="h-3.5 w-3.5" />} />
                  <ConfirmDangerButton onConfirm={() => void remove(p.id)} icon={<Trash2 className="h-3 w-3" />} ariaLabel={`Delete ${p.name}`} confirmLabel="Confirm Delete" />
                </div>
              </div>
              <Terms label="Must contain" terms={p.required} />
              <Terms label="Must not contain" terms={p.ignored} />
            </MachinedCard>
          ))}
        </div>
      )}
      <ReleaseProfileEditorModal
        target={editing}
        indexers={indexers}
        qualityProfiles={qualityProfiles}
        onClose={() => setEditing(null)}
        onSave={save}
      />
    </ProfileSection>
  );
};
