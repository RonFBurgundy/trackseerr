import React, { useState } from 'react';
import { Copy, Pencil, Star, Trash2 } from 'lucide-react';
import { ConfirmDangerButton, MachinedCard, TapeDeckButton } from '@/components/ui';
import type { CustomFormat } from '@/types/customFormats';
import type { QualityDefinition } from '@/types/qualityDefinitions';
import type { QualityProfile } from '@/types/qualityProfiles';
import type { UseQualityProfilesReturn } from '@/hooks/useQualityProfiles';
import { entryLabel } from '@/hooks/useQualityProfileDraft';
import { Badge, EmptyNote, ProfileSection } from './ProfileSection';
import { QualityProfileEditorModal, type QualityProfileTarget } from './QualityProfileEditorModal';

export interface QualityProfilesSectionProps {
  manager: UseQualityProfilesReturn;
  definitions: readonly QualityDefinition[];
  formats: readonly CustomFormat[];
}

function summary(p: QualityProfile, titleOf: (q: string) => string): string {
  const allowed = p.items.filter((e) => e.allowed).map((e) => (e.type === 'group' ? e.name : titleOf(e.quality)));
  return allowed.length > 0 ? allowed.join(', ') : 'Nothing allowed';
}

export const QualityProfilesSection: React.FC<QualityProfilesSectionProps> = ({ manager, definitions, formats }) => {
  const { profiles, loading, save, copy, remove, makeDefault } = manager;
  const [editing, setEditing] = useState<QualityProfileTarget | null>(null);
  const titleOf = (quality: string): string => definitions.find((d) => d.quality === quality)?.title ?? quality;

  return (
    <ProfileSection
      title="Quality Profiles"
      hint="Which qualities are allowed, in what order of preference, and how custom formats score releases."
      addLabel="Add"
      onAdd={() => setEditing('new')}
    >
      {loading && profiles.length === 0 ? (
        <EmptyNote>Loading quality profiles...</EmptyNote>
      ) : profiles.length === 0 ? (
        <EmptyNote>No quality profiles defined yet.</EmptyNote>
      ) : (
        <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
          {profiles.map((p) => {
            const cutoffTitle = titleOf(p.cutoff);
            return (
              <MachinedCard key={p.id} className="space-y-2 p-2.5 sm:p-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="break-words text-sm font-bold text-white">{p.name}</span>
                      {p.is_default && <Badge tone="amber">Default</Badge>}
                    </div>
                    <p className="mt-1 text-xs font-mono text-neutral-400">
                      Cutoff: {cutoffTitle} | Upgrades {p.upgrade_allowed ? 'on' : 'off'} | {p.items.filter((e) => e.allowed).length}/
                      {p.items.length} allowed
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    <TapeDeckButton size="sm" aria-label={`Edit ${p.name}`} onClick={() => setEditing(p)} icon={<Pencil className="h-3.5 w-3.5" />} />
                    <TapeDeckButton size="sm" aria-label={`Copy ${p.name}`} onClick={() => void copy(p.id)} icon={<Copy className="h-3.5 w-3.5" />} />
                    <TapeDeckButton
                      size="sm"
                      aria-label={`Make ${p.name} the default`}
                      disabled={p.is_default}
                      onClick={() => void makeDefault(p.id)}
                      icon={<Star className="h-3.5 w-3.5" />}
                    />
                    <ConfirmDangerButton
                      disabled={p.is_default}
                      onConfirm={() => void remove(p.id)}
                      icon={<Trash2 className="h-3 w-3" />}
                      ariaLabel={`Delete ${p.name}`}
                      confirmLabel="Confirm Delete"
                    />
                  </div>
                </div>
                <p className="line-clamp-2 text-[11px] font-mono text-[var(--text-muted)]" title={p.items.map(entryLabel).join(', ')}>
                  {summary(p, titleOf)}
                </p>
              </MachinedCard>
            );
          })}
        </div>
      )}
      <QualityProfileEditorModal target={editing} definitions={definitions} formats={formats} onClose={() => setEditing(null)} onSave={save} />
    </ProfileSection>
  );
};
