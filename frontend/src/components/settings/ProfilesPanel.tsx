import React, { useState } from 'react';
import { Trash2, Plus } from 'lucide-react';
import { TapeDeckButton, MachinedCard, TactileSwitch, ConfirmDangerButton, ActionBar } from '@/components/ui';
import type { QualityProfile } from '@/types/models';
import { saveQualityProfile, deleteQualityProfile } from '@/services/settingsService';
import { compactInputClass, compactLabelClass } from './formClasses';

export interface ProfilesPanelProps {
  profiles: QualityProfile[];
  onProfilesChange: React.Dispatch<React.SetStateAction<QualityProfile[]>>;
  reload: () => Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

export const ProfilesPanel: React.FC<ProfilesPanelProps> = ({ profiles, onProfilesChange, reload, onToast }) => {
  const [name, setName] = useState<string>('');
  const [cutoff, setCutoff] = useState<number>(1);
  const [isSaving, setIsSaving] = useState<boolean>(false);

  const handleToggleUpgrade = async (profile: QualityProfile) => {
    const allowed = !profile.upgrade_allowed;
    onProfilesChange((prev) => prev.map((pr) => (pr.id === profile.id ? { ...pr, upgrade_allowed: allowed } : pr)));
    try {
      await saveQualityProfile({ ...profile, upgrade_allowed: allowed });
      onToast(`Updated ${profile.name}`);
    } catch {
      onToast('Failed to update quality profile', 'error');
      void reload();
    }
  };

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setIsSaving(true);
    try {
      await saveQualityProfile({ name: name.trim(), cutoff, upgrade_allowed: true });
      setName('');
      await reload();
      onToast('Quality profile created');
    } catch {
      onToast('Failed to save profile', 'error');
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await deleteQualityProfile(id);
      await reload();
      onToast('Profile deleted');
    } catch {
      onToast('Failed to delete profile', 'error');
    }
  };

  return (
    <div className="space-y-6">
      {profiles.length === 0 ? (
        <p className="text-xs font-mono text-neutral-500 py-4">No quality profiles defined yet.</p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {profiles.map((p) => (
            <MachinedCard key={p.id} className="p-4 flex items-center justify-between gap-3">
              <div>
                <span className="font-bold text-sm text-white">{p.name}</span>
                <p className="text-xs text-neutral-400 font-mono mt-1">Cutoff Tier: {p.cutoff}</p>
              </div>
              <div className="flex items-center gap-2">
                <TactileSwitch
                  checked={Boolean(p.upgrade_allowed)}
                  onChange={() => void handleToggleUpgrade(p)}
                  label="Upgrade"
                />
                <ConfirmDangerButton
                  onConfirm={() => void handleDelete(p.id)}
                  icon={<Trash2 className="h-3 w-3" />}
                  ariaLabel="Delete profile"
                  confirmLabel="Confirm Delete"
                />
              </div>
            </MachinedCard>
          ))}
        </div>
      )}

      <MachinedCard className="p-5 max-w-xl">
        <h4 className="text-xs font-bold uppercase font-mono text-white mb-4">Add Quality Profile</h4>
        <form onSubmit={handleAdd} className="space-y-4">
          <div>
            <label className={compactLabelClass}>Profile Name</label>
            <input
              type="text"
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. FLAC Lossless Only"
              className={compactInputClass}
            />
          </div>
          <div>
            <label className={compactLabelClass}>Cutoff Score / Level</label>
            <input
              type="number"
              required
              value={cutoff}
              onChange={(e) => setCutoff(Number(e.target.value))}
              className={compactInputClass}
            />
          </div>
          <ActionBar align="end" className="pt-2">
            <TapeDeckButton
              type="submit"
              size="sm"
              variant="amber"
              disabled={isSaving}
              icon={<Plus className="h-3.5 w-3.5" />}
            >
              Create Profile
            </TapeDeckButton>
          </ActionBar>
        </form>
      </MachinedCard>
    </div>
  );
};
