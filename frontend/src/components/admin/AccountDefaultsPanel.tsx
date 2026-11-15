import React, { useEffect, useState } from 'react';
import { Loader2, Save } from 'lucide-react';
import {
  FormField,
  MachinedCard,
  StatusMessage,
  TactileSwitch,
  TapeDeckButton,
  inputClass,
} from '@/components/ui';
import { PermissionSwitches } from './PermissionSwitches';
import type { AccountDefaultSettings } from '@/types/account';
import { errorMessage } from '@/services/apiClient';

export interface AccountDefaultsPanelProps {
  defaults: AccountDefaultSettings | null;
  onSave: (next: AccountDefaultSettings) => Promise<void>;
}

type NumericKey = Exclude<
  keyof AccountDefaultSettings,
  'require_mfa_local' | 'default_user_permissions'
>;

const FIELDS: ReadonlyArray<{ key: NumericKey; label: string }> = [
  { key: 'default_quota_tracks', label: 'Tracks per window' },
  { key: 'default_quota_albums', label: 'Albums per window' },
  { key: 'default_quota_discographies', label: 'Discographies per window' },
  { key: 'default_quota_window_days', label: 'Window (days)' },
];

export const AccountDefaultsPanel: React.FC<AccountDefaultsPanelProps> = ({ defaults, onSave }) => {
  const [draft, setDraft] = useState<AccountDefaultSettings | null>(defaults);
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<boolean>(false);

  useEffect(() => {
    setDraft(defaults);
  }, [defaults]);

  if (!draft) return null;

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSaving(true);
    setError(null);
    setSaved(false);
    try {
      await onSave(draft);
      setSaved(true);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to save defaults'));
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <MachinedCard className="p-3 sm:p-6 max-w-2xl">
      <form onSubmit={handleSave} className="space-y-5">
        <h3 className="text-sm font-bold uppercase tracking-wider">Account defaults</h3>
        <TactileSwitch
          label="Require MFA for local accounts"
          checked={draft.require_mfa_local}
          onChange={(v) => setDraft({ ...draft, require_mfa_local: v })}
        />
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {FIELDS.map((f) => (
            <FormField key={f.key} label={f.label} htmlFor={`def-${f.key}`}>
              <input
                id={`def-${f.key}`}
                type="number"
                min={f.key === 'default_quota_window_days' ? 1 : 0}
                value={draft[f.key]}
                onChange={(e) => {
                  const n = Number.parseInt(e.target.value, 10);
                  setDraft({ ...draft, [f.key]: Number.isNaN(n) ? 0 : n });
                }}
                className={inputClass}
              />
            </FormField>
          ))}
        </div>
        <fieldset>
          <legend className="text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] mb-2">
            Default permissions for new users
          </legend>
          <p className="text-[11px] font-mono text-[var(--text-muted)] mb-2">
            Given to people the first time they sign in with Plex or are added from your media server.
            Existing users don't change.
          </p>
          <PermissionSwitches
            idPrefix="default-permission"
            value={draft.default_user_permissions}
            onChange={(next) => setDraft({ ...draft, default_user_permissions: next })}
            hideAdmin
          />
        </fieldset>
        {error && <StatusMessage variant="error">{error}</StatusMessage>}
        {saved && <StatusMessage variant="success">Defaults saved.</StatusMessage>}
        <TapeDeckButton
          type="submit"
          variant="amber"
          disabled={isSaving}
          icon={isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
        >
          Save defaults
        </TapeDeckButton>
      </form>
    </MachinedCard>
  );
};
