import React, { useEffect, useState } from 'react';
import { Loader2, Save } from 'lucide-react';
import { FormField, ObsidianModal, StatusMessage, TapeDeckButton, inputClass } from '@/components/ui';
import type { LogSettings, LogSettingsUpdate } from '@/types/models';
import { useLogSettings } from '@/hooks/useLogSettings';

export interface LogSettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
  /** Called with a message after a successful save or on a failed one. */
  onResult: (message: string, tone: 'ok' | 'error') => void;
}

interface Draft {
  rotationHours: string;
  retentionDays: string;
  maxTotalMb: string;
  level: string;
}

const toDraft = (s: LogSettings): Draft => ({
  rotationHours: String(s.log_rotation_hours),
  retentionDays: String(s.log_retention_days),
  maxTotalMb: String(s.log_max_total_mb),
  level: s.log_level,
});

/** Rotation interval, retention, total size cap and level. Saved values apply live, no restart. */
export const LogSettingsModal: React.FC<LogSettingsModalProps> = React.memo(({ isOpen, onClose, onResult }) => {
  const { settings, isLoading, isSaving, error, save } = useLogSettings(isOpen);
  const [draft, setDraft] = useState<Draft | null>(null);

  useEffect(() => {
    if (settings) setDraft(toDraft(settings));
  }, [settings]);

  const retention = draft ? Number(draft.retentionDays) : NaN;
  const retentionValid =
    settings !== null &&
    Number.isInteger(retention) &&
    retention >= settings.retention_days_min &&
    retention <= settings.retention_days_max;

  const handleSave = async (): Promise<void> => {
    if (!settings || !draft || !retentionValid) return;
    const update: LogSettingsUpdate = {
      log_rotation_hours: Number(draft.rotationHours),
      log_retention_days: retention,
      log_max_total_mb: Number(draft.maxTotalMb),
      log_level: draft.level,
    };
    const failure = await save(update);
    if (failure === null) {
      onResult('Log settings saved', 'ok');
      onClose();
    } else {
      onResult(failure, 'error');
    }
  };

  const levelNote =
    draft && draft.level === 'TRACE'
      ? 'TRACE is extremely verbose. The total size cap keeps it from filling the disk.'
      : undefined;

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="Log Settings"
      subtitle="Applied immediately, no restart needed"
      maxWidth="sm:max-w-lg"
      footer={
        <>
          <TapeDeckButton size="sm" onClick={onClose}>
            Cancel
          </TapeDeckButton>
          <TapeDeckButton
            size="sm"
            variant="amber"
            onClick={() => void handleSave()}
            disabled={isSaving || isLoading || !draft || !retentionValid}
            icon={isSaving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
          >
            Save
          </TapeDeckButton>
        </>
      }
    >
      {error && <StatusMessage variant="error" className="mb-3">{error}</StatusMessage>}
      {isLoading && !draft && (
        <div className="py-8 flex justify-center text-[var(--text-muted)]">
          <Loader2 className="h-5 w-5 animate-spin" aria-label="Loading log settings" />
        </div>
      )}
      {settings && draft && (
        <div className="space-y-4">
          <FormField label="Rotate log file every" name="log_rotation_hours" hint="A new file starts after this long.">
            <select
              className={inputClass}
              value={draft.rotationHours}
              onChange={(e) => setDraft({ ...draft, rotationHours: e.target.value })}
            >
              {settings.rotation_hours_options.map((h) => (
                <option key={h} value={String(h)}>
                  {h} hours
                </option>
              ))}
            </select>
          </FormField>
          <FormField
            label="Keep rotated logs for (days)"
            name="log_retention_days"
            hint={`${settings.retention_days_min} to ${settings.retention_days_max} days.`}
          >
            <input
              type="number"
              inputMode="numeric"
              min={settings.retention_days_min}
              max={settings.retention_days_max}
              step={1}
              className={inputClass}
              value={draft.retentionDays}
              onChange={(e) => setDraft({ ...draft, retentionDays: e.target.value })}
            />
          </FormField>
          <FormField label="Total size cap" name="log_max_total_mb" hint="Oldest rotated files are deleted first.">
            <select
              className={inputClass}
              value={draft.maxTotalMb}
              onChange={(e) => setDraft({ ...draft, maxTotalMb: e.target.value })}
            >
              {settings.max_total_mb_options.map((mb) => (
                <option key={mb} value={String(mb)}>
                  {mb} MB
                </option>
              ))}
            </select>
          </FormField>
          <FormField label="Log level" name="log_level" hint={levelNote}>
            <select
              className={inputClass}
              value={draft.level}
              onChange={(e) => setDraft({ ...draft, level: e.target.value })}
            >
              {[...new Set([...settings.level_options, settings.log_level])].map((lvl) => (
                <option key={lvl} value={lvl}>
                  {lvl}
                </option>
              ))}
            </select>
          </FormField>
        </div>
      )}
    </ObsidianModal>
  );
});
LogSettingsModal.displayName = 'LogSettingsModal';
