import React, { useState, useEffect } from 'react';
import { Loader2, AlertTriangle, ChevronDown, ChevronRight, Check } from 'lucide-react';
import { MachinedCard, TapeDeckButton, FormField, inputClass } from '@/components/ui';
import { formatRelativeTime } from '@/components/deployment';
import { useSystemHealth, type HealthSettings } from '@/hooks/useSystemHealth';

export const SystemHealthCard: React.FC = () => {
  const {
    health,
    settings,
    loading,
    checking,
    saving,
    error,
    runNow,
    saveSettings,
  } = useSystemHealth(true);

  const [thresholdsExpanded, setThresholdsExpanded] = useState<boolean>(false);
  const [saveStatus, setSaveStatus] = useState<string | null>(null);

  const [thresholds, setThresholds] = useState<HealthSettings>({
    indexer_fail_minutes: 15,
    client_fail_minutes: 5,
    media_server_fail_minutes: 10,
    disk_free_min_gb: 20,
    download_stall_minutes: 60,
    renotify_hours: 24,
  });

  useEffect(() => {
    if (settings) {
      setThresholds(settings);
    }
  }, [settings]);

  const handleInputChange = (field: keyof HealthSettings) => (
    e: React.ChangeEvent<HTMLInputElement>
  ) => {
    const num = Number(e.target.value);
    setThresholds((prev) => ({
      ...prev,
      [field]: Number.isNaN(num) ? 0 : num,
    }));
  };

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>): Promise<void> => {
    e.preventDefault();
    try {
      await saveSettings(thresholds);
      setSaveStatus('Saved');
      setTimeout(() => setSaveStatus(null), 3000);
    } catch {
      // error is captured by useSystemHealth hook
    }
  };

  const checks = health?.checks ?? [];

  return (
    <MachinedCard className="p-3 sm:p-6 max-w-2xl space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h4 className="text-sm font-bold uppercase font-mono text-white">Health</h4>
        <TapeDeckButton
          type="button"
          size="sm"
          onClick={() => void runNow()}
          disabled={checking || loading}
          icon={checking ? <Loader2 className="h-3.5 w-3.5 animate-spin text-[var(--accent-amber)]" /> : undefined}
        >
          {checking ? 'Checking...' : 'Check now'}
        </TapeDeckButton>
      </div>

      {loading && !health && !error && (
        <div className="flex items-center gap-2 text-xs font-mono text-neutral-400">
          <Loader2 className="h-4 w-4 animate-spin text-[#e5a00d]" /> Reading system health...
        </div>
      )}

      {error && (
        <p className="flex items-start gap-2 text-xs font-mono text-red-300">
          <AlertTriangle className="h-4 w-4 shrink-0" /> {error}
        </p>
      )}

      {!loading && checks.length === 0 && !error && (
        <p className="text-xs font-mono text-neutral-500 py-1">
          No checks yet. They run every 5 minutes.
        </p>
      )}

      {checks.length > 0 && (
        <div className="divide-y divide-[#1f1f1f] text-xs font-mono">
          {checks.map((check) => {
            const isFailing = check.state === 'warning' || check.state === 'error';
            const dotColor =
              check.state === 'ok'
                ? 'bg-green-400'
                : check.state === 'warning'
                ? 'bg-amber-400'
                : check.state === 'error'
                ? 'bg-red-400'
                : 'bg-neutral-500';

            const stateColor =
              check.state === 'ok'
                ? 'text-green-400'
                : check.state === 'warning'
                ? 'text-amber-400'
                : check.state === 'error'
                ? 'text-red-400'
                : 'text-neutral-500';

            return (
              <div key={check.key} className="py-2.5 flex items-start justify-between gap-3">
                <div className="flex items-start gap-2.5 min-w-0">
                  <span
                    className={`inline-block h-2 w-2 rounded-full shrink-0 mt-1.5 ${dotColor}`}
                    aria-hidden="true"
                  />
                  <div className="min-w-0 space-y-0.5">
                    <div className="flex items-baseline gap-2 flex-wrap">
                      <span className="text-white font-medium">{check.label}</span>
                      {isFailing && check.failing_since && (
                        <span className="text-[11px] text-neutral-400">
                          since {formatRelativeTime(check.failing_since)}
                        </span>
                      )}
                    </div>
                    {check.message && (
                      <p className="text-neutral-400 text-xs leading-relaxed break-words">
                        {check.message}
                      </p>
                    )}
                  </div>
                </div>
                <span className={`uppercase text-[11px] font-semibold shrink-0 ${stateColor}`}>
                  {check.state}
                </span>
              </div>
            );
          })}
        </div>
      )}

      <div className="pt-2 border-t border-[#1f1f1f]">
        <button
          type="button"
          onClick={() => setThresholdsExpanded((v) => !v)}
          aria-expanded={thresholdsExpanded}
          aria-controls="system-health-thresholds"
          className="inline-flex items-center gap-1.5 min-h-[28px] text-[11px] font-mono font-bold uppercase tracking-wider text-neutral-400 hover:text-white focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--accent-amber)]"
        >
          {thresholdsExpanded ? (
            <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          Alert thresholds
        </button>
        <div id="system-health-thresholds">
          {thresholdsExpanded && (
            <form onSubmit={handleSubmit} className="mt-3 pt-3 border-t border-[#1f1f1f] space-y-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <FormField label="Indexer failing for (minutes)" htmlFor="health-indexer-fail-minutes">
                  <input
                    id="health-indexer-fail-minutes"
                    name="indexer_fail_minutes"
                    type="number"
                    min={0}
                    className={inputClass}
                    value={thresholds.indexer_fail_minutes}
                    onChange={handleInputChange('indexer_fail_minutes')}
                  />
                </FormField>
                <FormField label="Download client unreachable for (minutes)" htmlFor="health-client-fail-minutes">
                  <input
                    id="health-client-fail-minutes"
                    name="client_fail_minutes"
                    type="number"
                    min={0}
                    className={inputClass}
                    value={thresholds.client_fail_minutes}
                    onChange={handleInputChange('client_fail_minutes')}
                  />
                </FormField>
                <FormField label="Media server unreachable for (minutes)" htmlFor="health-media-server-fail-minutes">
                  <input
                    id="health-media-server-fail-minutes"
                    name="media_server_fail_minutes"
                    type="number"
                    min={0}
                    className={inputClass}
                    value={thresholds.media_server_fail_minutes}
                    onChange={handleInputChange('media_server_fail_minutes')}
                  />
                </FormField>
                <FormField label="Warn below free space (GB)" htmlFor="health-disk-free-min-gb">
                  <input
                    id="health-disk-free-min-gb"
                    name="disk_free_min_gb"
                    type="number"
                    min={0}
                    className={inputClass}
                    value={thresholds.disk_free_min_gb}
                    onChange={handleInputChange('disk_free_min_gb')}
                  />
                </FormField>
                <FormField label="Download stalled after (minutes)" htmlFor="health-download-stall-minutes">
                  <input
                    id="health-download-stall-minutes"
                    name="download_stall_minutes"
                    type="number"
                    min={0}
                    className={inputClass}
                    value={thresholds.download_stall_minutes}
                    onChange={handleInputChange('download_stall_minutes')}
                  />
                </FormField>
                <FormField label="Repeat an unresolved alert every (hours)" htmlFor="health-renotify-hours">
                  <input
                    id="health-renotify-hours"
                    name="renotify_hours"
                    type="number"
                    min={0}
                    className={inputClass}
                    value={thresholds.renotify_hours}
                    onChange={handleInputChange('renotify_hours')}
                  />
                </FormField>
              </div>

              <div className="flex items-center gap-3 pt-1">
                <TapeDeckButton type="submit" size="sm" variant="amber" disabled={saving}>
                  {saving ? 'Saving...' : 'Save'}
                </TapeDeckButton>
                {saveStatus && (
                  <span className="text-xs font-mono text-green-400 flex items-center gap-1">
                    <Check className="h-3.5 w-3.5" /> {saveStatus}
                  </span>
                )}
              </div>

              <p className="text-xs font-mono text-[var(--text-secondary)]">
                Choose which alerts you get in Settings → Notifications and in your account's notification settings.
              </p>
            </form>
          )}
        </div>
      </div>
    </MachinedCard>
  );
};
