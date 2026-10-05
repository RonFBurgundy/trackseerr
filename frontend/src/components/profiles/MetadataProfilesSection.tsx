import React, { useId, useState } from 'react';
import { Pencil, Trash2 } from 'lucide-react';
import { ConfirmDangerButton, FormField, MachinedCard, TapeDeckButton } from '@/components/ui';
import type { MediaManagementSettings } from '@/types/models';
import {
  RELEASE_PRIMARY_LABELS,
  RELEASE_SECONDARY_LABELS,
  type ReleaseOrNew,
  type MetadataProfile,
} from '@/types/metadataProfiles';
import { updateMediaManagementSettings } from '@/services/settingsService';
import { errorMessage } from '@/services/apiClient';
import { useMetadataProfiles } from '@/hooks/useMetadataProfiles';
import { inputClass } from '@/components/settings/formClasses';
import { EmptyNote, ProfileSection } from './ProfileSection';
import { MetadataProfileEditorModal } from './MetadataProfileEditorModal';

export interface MetadataProfilesSectionProps {
  /** False while Lidarr manages the library: the routes answer 409 there, so nothing is fetched. */
  enabled: boolean;
  settings: MediaManagementSettings | null;
  onChange: React.Dispatch<React.SetStateAction<MediaManagementSettings | null>>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

const NONE = '';

function summarize(p: MetadataProfile): string {
  const primary = p.primary_types.map((t) => RELEASE_PRIMARY_LABELS[t]).join(', ');
  const secondary = p.secondary_types.map((t) => RELEASE_SECONDARY_LABELS[t].replace(' (no secondary type)', '')).join(', ');
  return `${primary} / ${secondary}`;
}

/** Optional metadata profiles for the native library: they shape automatic monitoring and never hide releases. */
export const MetadataProfilesSection: React.FC<MetadataProfilesSectionProps> = ({ enabled, settings, onChange, onToast }) => {
  const defaultId = useId();
  const { profiles, loading, save, remove } = useMetadataProfiles(enabled, onToast);
  const [editing, setEditing] = useState<ReleaseOrNew | null>(null);

  const currentDefault = settings?.add_metadata_profile_id ?? null;

  const changeDefault = async (raw: string): Promise<void> => {
    const next = raw === NONE ? null : Number(raw);
    try {
      const updated = await updateMediaManagementSettings({ add_metadata_profile_id: next });
      onChange((prev) => (prev ? { ...prev, add_metadata_profile_id: updated.add_metadata_profile_id ?? null } : prev));
      onToast(next === null ? 'New artists get no metadata profile' : 'Default metadata profile saved');
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to save default metadata profile'), 'error');
    }
  };

  const handleDelete = async (id: number): Promise<void> => {
    const cleared = await remove(id);
    if (cleared !== null && currentDefault === id) {
      onChange((prev) => (prev ? { ...prev, add_metadata_profile_id: null } : prev));
    }
  };

  return (
    <ProfileSection
      title="Metadata Profiles"
      hint="Optional. Decide which releases are monitored automatically; every release stays in the catalog and can be monitored by hand."
      onAdd={() => setEditing('new')}
    >
      <MachinedCard className="space-y-2 p-3">
        <p className="text-xs font-mono text-neutral-400">
          Off by default. Files you already own stay monitored under &ldquo;Existing tracks&rdquo; whatever the profile says.
        </p>
        <FormField label="Default for new artists" htmlFor={defaultId} className="sm:w-72">
          <select
            id={defaultId}
            name="add_metadata_profile_id"
            className={inputClass}
            value={currentDefault === null ? NONE : String(currentDefault)}
            disabled={settings === null}
            onChange={(e) => void changeDefault(e.target.value)}
          >
            <option value={NONE}>No profile</option>
            {profiles.map((p) => (
              <option key={p.id} value={String(p.id)}>
                {p.name}
              </option>
            ))}
          </select>
        </FormField>
      </MachinedCard>

      {loading && profiles.length === 0 ? (
        <EmptyNote>Loading metadata profiles...</EmptyNote>
      ) : profiles.length === 0 ? (
        <EmptyNote>No metadata profiles defined. Artists are monitored by their option alone.</EmptyNote>
      ) : (
        <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
          {profiles.map((p) => (
            <MachinedCard key={p.id} className="flex items-start justify-between gap-3 p-2.5 sm:p-3">
              <div className="min-w-0">
                <span className="break-words text-sm font-bold text-white">{p.name}</span>
                <p className="mt-1 break-words text-xs font-mono text-neutral-400">{summarize(p)}</p>
                <p className="mt-1 text-[11px] font-mono text-neutral-500">
                  {p.artist_count} {p.artist_count === 1 ? 'artist' : 'artists'}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <TapeDeckButton
                  size="sm"
                  aria-label={`Edit ${p.name}`}
                  onClick={() => setEditing(p)}
                  icon={<Pencil className="h-3.5 w-3.5" />}
                />
                <ConfirmDangerButton
                  onConfirm={() => void handleDelete(p.id)}
                  icon={<Trash2 className="h-3 w-3" />}
                  ariaLabel={`Delete ${p.name}`}
                  confirmLabel={p.artist_count > 0 ? `Clear ${p.artist_count} artists` : 'Confirm Delete'}
                />
              </div>
            </MachinedCard>
          ))}
        </div>
      )}

      <MetadataProfileEditorModal target={editing} onClose={() => setEditing(null)} onSave={save} />
    </ProfileSection>
  );
};
