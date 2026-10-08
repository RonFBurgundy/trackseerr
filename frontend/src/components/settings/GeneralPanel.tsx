import React, { useEffect, useState } from 'react';
import { AlertTriangle, Check, Copy, Eye, EyeOff, Loader2, RotateCw, Save } from 'lucide-react';
import { TapeDeckButton, MachinedCard, ActionBar } from '@/components/ui';
import type { GeneralSettings } from '@/types/models';
import { getApiKey, regenerateApiKey, updateGeneralSettings } from '@/services/settingsService';
import { inputClass, labelClass } from './formClasses';

export interface GeneralPanelProps {
  settings: GeneralSettings | null;
  onChange: React.Dispatch<React.SetStateAction<GeneralSettings | null>>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
  isAdmin?: boolean;
  isGateway?: boolean;
}

export const GeneralPanel: React.FC<GeneralPanelProps> = ({
  settings,
  onChange,
  onToast,
  isAdmin = true,
  isGateway = false,
}) => {
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [apiKey, setApiKey] = useState<string>('');
  const [isLoadingKey, setIsLoadingKey] = useState<boolean>(true);
  const [showKey, setShowKey] = useState<boolean>(false);
  const [copied, setCopied] = useState<boolean>(false);
  const [isRegenerating, setIsRegenerating] = useState<boolean>(false);
  const [confirmRegenerate, setConfirmRegenerate] = useState<boolean>(false);

  const showApiKeyBlock = isAdmin && !isGateway;

  useEffect(() => {
    if (!showApiKeyBlock) return;
    let active = true;
    getApiKey()
      .then((res) => {
        if (active) setApiKey(res.api_key);
      })
      .catch(() => {
        if (active) onToast('Failed to load API key', 'error');
      })
      .finally(() => {
        if (active) setIsLoadingKey(false);
      });
    return () => {
      active = false;
    };
  }, [showApiKeyBlock, onToast]);

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

  const handleCopy = async () => {
    if (!apiKey) return;
    try {
      await navigator.clipboard.writeText(apiKey);
      setCopied(true);
      onToast('API key copied to clipboard');
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      onToast('Failed to copy API key', 'error');
    }
  };

  const handleRegenerate = async () => {
    setIsRegenerating(true);
    try {
      const res = await regenerateApiKey();
      setApiKey(res.api_key);
      setConfirmRegenerate(false);
      onToast(res.message || 'API key successfully regenerated');
    } catch {
      onToast('Failed to regenerate API key', 'error');
    } finally {
      setIsRegenerating(false);
    }
  };

  return (
    <div className="space-y-6">
      <MachinedCard className="p-3 sm:p-6 max-w-2xl">
        <form onSubmit={handleSave} className="space-y-5">
          <div>
            <label htmlFor="general-application-url" className={labelClass}>Application URL</label>
            <input
              id="general-application-url"
              name="application_url"
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

      {showApiKeyBlock && (
        <MachinedCard className="p-3 sm:p-6 max-w-2xl space-y-4">
          <div>
            <h3 className="text-sm font-bold text-white uppercase tracking-wider mb-1">API Key</h3>
            <p className="text-[11px] font-mono text-neutral-400">
              Machine key for external integrations and scripts to authenticate with TrackSeerr.
            </p>
          </div>

          <div className="space-y-2">
            <label htmlFor="general-api-key" className={labelClass}>API Key</label>
            <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2">
              <input
                id="general-api-key"
                name="api_key"
                type={showKey ? 'text' : 'password'}
                value={isLoadingKey ? 'Loading...' : apiKey}
                readOnly
                aria-label="API Key"
                className={`${inputClass} flex-1 font-mono tracking-wider`}
              />
              <div className="flex items-center gap-2 shrink-0">
                <TapeDeckButton
                  type="button"
                  size="sm"
                  disabled={isLoadingKey || !apiKey}
                  onClick={() => setShowKey((prev) => !prev)}
                  icon={showKey ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                  aria-label={showKey ? 'Hide API key' : 'Show API key'}
                >
                  {showKey ? 'Hide' : 'Show'}
                </TapeDeckButton>

                <TapeDeckButton
                  type="button"
                  size="sm"
                  disabled={isLoadingKey || !apiKey}
                  onClick={() => void handleCopy()}
                  icon={copied ? <Check className="h-3.5 w-3.5 text-[var(--status-success)]" /> : <Copy className="h-3.5 w-3.5" />}
                  aria-label="Copy API key"
                >
                  {copied ? 'Copied' : 'Copy'}
                </TapeDeckButton>

                {!confirmRegenerate && (
                  <TapeDeckButton
                    type="button"
                    variant="danger"
                    size="sm"
                    disabled={isLoadingKey || isRegenerating}
                    onClick={() => setConfirmRegenerate(true)}
                    icon={<RotateCw className="h-3.5 w-3.5" />}
                    aria-label="Regenerate API key"
                  >
                    Regenerate
                  </TapeDeckButton>
                )}
              </div>
            </div>
          </div>

          {confirmRegenerate && (
            <div className="p-3 bg-red-950/40 border border-red-800/60 rounded-[4px] space-y-2 text-xs">
              <div className="flex items-center gap-2 text-red-300 font-bold">
                <AlertTriangle className="h-4 w-4 text-red-400 shrink-0" />
                <span>Existing scripts will stop working</span>
              </div>
              <p className="text-red-200/80 font-mono text-[11px]">
                Regenerating will immediately revoke this API key. Any external scripts, automations, or tools configured with this key will stop working until updated.
              </p>
              <div className="flex items-center gap-2 pt-1">
                <TapeDeckButton
                  type="button"
                  variant="danger"
                  size="sm"
                  disabled={isRegenerating}
                  onClick={() => void handleRegenerate()}
                  icon={isRegenerating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCw className="h-3.5 w-3.5" />}
                >
                  Confirm Regenerate
                </TapeDeckButton>
                <TapeDeckButton
                  type="button"
                  size="sm"
                  disabled={isRegenerating}
                  onClick={() => setConfirmRegenerate(false)}
                >
                  Cancel
                </TapeDeckButton>
              </div>
            </div>
          )}
        </MachinedCard>
      )}
    </div>
  );
};
