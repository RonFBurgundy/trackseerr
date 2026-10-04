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
      await updateGeneralSettings(settings);
      onToast('General settings saved successfully');
    } catch {
      onToast('Failed to save settings', 'error');
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <MachinedCard className="p-6 max-w-2xl">
      <form onSubmit={handleSave} className="space-y-5">
        <div>
          <label className={labelClass}>Server Name</label>
          <input
            type="text"
            value={settings?.server_name || ''}
            onChange={(e) => onChange((prev) => (prev ? { ...prev, server_name: e.target.value } : null))}
            className={inputClass}
          />
        </div>
        <div>
          <label className={labelClass}>Base URL</label>
          <input
            type="text"
            value={settings?.base_url || ''}
            onChange={(e) => onChange((prev) => (prev ? { ...prev, base_url: e.target.value } : null))}
            className={inputClass}
          />
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
