import React, { useId, useState } from 'react';
import { Pencil, Plus, Trash2 } from 'lucide-react';
import { ActionBar, ConfirmDangerButton, FormField, MachinedCard, ScrollFill, TapeDeckButton } from '@/components/ui';
import type { MediaManagementSettings } from '@/types/models';
import {
  RELEASE_PRIMARY_LABELS,
  RELEASE_SECONDARY_LABELS,
  type ReleaseOrNew,
  type ReleaseProfile,
} from '@/types/releaseProfiles';
import { updateMediaManagementSettings } from '@/services/settingsService';
import { errorMessage } from '@/services/apiClient';
import { useReleaseProfiles } from '@/hooks/useReleaseProfiles';
import { inputClass } from './formClasses';
import { ReleaseProfileEditorModal } from './ReleaseProfileEditorModal';

export interface ReleaseProfilesPanelProps {
  /** False while Lidarr manages the library: the routes answer 409 there, so nothing is fetched. */
  enabled: boolean;
  settings: MediaManagementSettings | null;
  onChange: React.Dispatch<React.SetStateAction<MediaManagementSettings | null>>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

const NONE = '';

function summarize(p: ReleaseProfile): string {
  const primary = p.primary_types.map((t) => RELEASE_PRIMARY_LABELS[t]).join(', ');
  const secondary = p.secondary_types.map((t) => RELEASE_SECONDARY_LABELS[t].replace(' (no secondary type)', '')).join(', ');
  return `${primary} / ${secondary}`;
}

/** Optional release profiles for the native library: they shape automatic monitoring and never hide releases. */
export const ReleaseProfilesPanel: React.FC<ReleaseProfilesPanelProps> = ({ enabled, settings, onChange, onToast }) => {
  const defaultId = useId();
  const { profiles, loading, save, remove } = useReleaseProfiles(enabled, onToast);
  const [editing, setEditing] = useState<ReleaseOrNew | null>(null);

  const currentDefault = settings?.add_release_profile_id ?? null;

  const changeDefault = async (raw: string): Promise<void> => {
    const next = raw === NONE ? null : Number(raw);
    try {
      const updated = await updateMediaManagementSettings({ add_release_profile_id: next });
      onChange((prev) => (prev ? { ...prev, add_release_profile_id: updated.add_release_profile_id ?? null } : prev));
      onToast(next === null ? 'New artists get no release profile' : 'Default release profile saved');
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to save default release profile'), 'error');
    }
  };

  const handleDelete = async (id: number): Promise<void> => {
    const cleared = await remove(id);
    if (cleared !== null && currentDefault === id) {
      onChange((prev) => (prev ? { ...prev, add_release_profile_id: null } : prev));
    }
  };

  return (
    <div className="flex min-h-0 flex-col gap-4">
      <MachinedCard className="p-3 sm:p-4 space-y-3">
        <p className="text-xs font-mono text-neutral-400">
          Release profiles are optional and off by default. They only decide which releases are monitored
          automatically; every release stays in the catalog and can always be monitored or requested by hand. Files
          you already own stay monitored under &ldquo;Existing tracks&rdquo; whatever the profile says.
        </p>
        <div className="flex flex-col sm:flex-row sm:items-end gap-3">
          <FormField label="Default for new artists" htmlFor={defaultId} className="sm:w-72">
            <select
              id={defaultId}
              name="add_release_profile_id"
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
          <ActionBar align="end" className="sm:ml-auto">
            <TapeDeckButton variant="amber" onClick={() => setEditing('new')} icon={<Plus className="h-4 w-4" />}>
              New profile
            </TapeDeckButton>
          </ActionBar>
        </div>
      </MachinedCard>

      {loading && profiles.length === 0 ? (
        <p className="text-xs font-mono text-neutral-500 py-4">Loading release profiles...</p>
      ) : profiles.length === 0 ? (
        <p className="text-xs font-mono text-neutral-500 py-4">No release profiles defined. Artists are monitored by their option alone.</p>
      ) : (
        <ScrollFill ariaLabel="Release profiles" className="grid grid-cols-1 md:grid-cols-2 gap-3 content-start">
          {profiles.map((p) => (
            <MachinedCard key={p.id} className="p-3 sm:p-4 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <span className="font-bold text-sm text-white break-words">{p.name}</span>
                <p className="text-xs text-neutral-400 font-mono mt-1 break-words">{summarize(p)}</p>
                <p className="text-[11px] text-neutral-500 font-mono mt-1">
                  {p.artist_count} {p.artist_count === 1 ? 'artist' : 'artists'}
                </p>
              </div>
              <div className="flex items-center gap-2 shrink-0">
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
        </ScrollFill>
      )}

      <ReleaseProfileEditorModal target={editing} onClose={() => setEditing(null)} onSave={save} />
    </div>
  );
};
