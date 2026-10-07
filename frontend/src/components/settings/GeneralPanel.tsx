import React, { useState } from 'react';
import { Loader2, Save } from 'lucide-react';
import { TapeDeckButton, MachinedCard, ActionBar } from '@/components/ui';
import type { GeneralSettings } from '@/types/models';
import { updateGeneralSettings } from '@/services/settingsService';
import { inputClass, labelClass } from './formClasses';

export interface GeneralPanelProps {
  settings: GeneralSettings | null;
  onChange: React.Dispatch<React.SetStateAction<GeneralSettings | null>>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

export const GeneralPanel: React.FC<GeneralPanelProps> = ({ settings, onChange, onToast }) => {
  const [isSaving, setIsSaving] = useState<boolean>(false);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!settings) return;
    setIsSaving(true);
    try {
      const saved = await updateGeneralSettings({ application_url: settings.application_url });
      onChange(saved);
      onToast('General settings saved successfully');
    } catch {
      onToast('Failed to save settings', 'error');
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <MachinedCard className="p-3 sm:p-6 max-w-2xl">
      <form onSubmit={handleSave} className="space-y-5">
        <div>
          <label htmlFor="general-application-url" className={labelClass}>Application URL</label>
          <input id="general-application-url" name="application_url"
            type="url"
            value={settings?.application_url ?? ''}
            onChange={(e) => onChange((prev) => (prev ? { ...prev, application_url: e.target.value } : null))}
            placeholder="https://trackseerr.example.com"
            className={inputClass}
          />
          <p className="mt-1 text-[11px] font-mono text-neutral-500">
            External address used in invite links, Plex sign-in redirects and notifications.
          </p>
        </div>
        <ActionBar align="end" className="pt-3">
          <TapeDeckButton
            type="submit"
            variant="amber"
            size="md"
            disabled={isSaving}
            icon={isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          >
            Save General Settings
          </TapeDeckButton>
        </ActionBar>
      </form>
    </MachinedCard>
  );
};
