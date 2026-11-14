import React, { useEffect, useId, useState } from 'react';
import { ArrowRight, ExternalLink, Eye, EyeOff, Loader2, Save, Trash2 } from 'lucide-react';
import { ConfirmDialog, TapeDeckButton, MachinedCard, TactileSwitch, ActionBar } from '@/components/ui';
import type { MediaManagementSettings } from '@/types/models';
import type { Schema } from '@/types/apiSchema';
import { updateMediaManagementSettings, testMusicBrainzMirror } from '@/services/settingsService';
import { getAcoustidContributions } from '@/services/libraryService';
import { routeToHash, settingsRouteFor } from '@/hooks/useAppRoute';
import { ApiError } from '@/services/apiClient';
import { inputClass, labelClass } from './formClasses';

export interface MetadataSourcesPanelProps {
  settings: MediaManagementSettings | null;
  onChange: React.Dispatch<React.SetStateAction<MediaManagementSettings | null>>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** A server-masked secret starts with a run of mask characters (e.g. "\u2022\u2022\u2022\u20221234"). */
const isMaskedKey = (value: string | null | undefined): boolean => !!value && /^[*\u2022\u25CF]+/.test(value);

export const MetadataSourcesPanel: React.FC<MetadataSourcesPanelProps> = ({ settings, onChange, onToast }) => {
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const mbMirrorId = useId();
  const acoustidKeyId = useId();
  const acoustidUserKeyId = useId();
  const mbFromEnv = settings?.mb_mirror_url_from_env ?? false;
  const [isTestingMb, setIsTestingMb] = useState<boolean>(false);
  const [showKey, setShowKey] = useState<boolean>(false);
  const [confirmRemoveKey, setConfirmRemoveKey] = useState<boolean>(false);
  const [removingKey, setRemovingKey] = useState<boolean>(false);
  // null = untouched: the server value (possibly masked) is shown and never sent back.
  const [keyDraft, setKeyDraft] = useState<string | null>(null);
  const serverKey = settings?.acoustid_api_key ?? '';
  const keyMasked = keyDraft === null && isMaskedKey(serverKey);
  const keyValue = keyDraft ?? serverKey;
  const keyConfigured = keyValue.trim().length > 0;

  const [showUserKey, setShowUserKey] = useState<boolean>(false);
  const [confirmRemoveUserKey, setConfirmRemoveUserKey] = useState<boolean>(false);
  const [removingUserKey, setRemovingUserKey] = useState<boolean>(false);
  const [userKeyDraft, setUserKeyDraft] = useState<string | null>(null);
  const serverUserKey = settings?.acoustid_user_key ?? '';
  const userKeyMasked = userKeyDraft === null && isMaskedKey(serverUserKey);
  const userKeyValue = userKeyDraft ?? serverUserKey;
  const userKeyConfigured = userKeyValue.trim().length > 0;

  const [contributions, setContributions] = useState<Schema<'AcoustidContributionsResponse'> | null>(null);
  const [loadingContributions, setLoadingContributions] = useState<boolean>(false);

  useEffect(() => {
    let active = true;
    const loadContributions = async () => {
      setLoadingContributions(true);
      try {
        const res = await getAcoustidContributions();
        if (active) {
          setContributions(res);
        }
      } catch {
        if (active) {
          setContributions(null);
        }
      } finally {
        if (active) {
          setLoadingContributions(false);
        }
      }
    };

    if (userKeyConfigured) {
      void loadContributions();
    } else {
      setContributions(null);
    }

    return () => {
      active = false;
    };
  }, [userKeyConfigured]);

  const handleRemoveKey = async (): Promise<void> => {
    setRemovingKey(true);
    try {
      // An explicit empty string clears the stored key server-side.
      const updated = await updateMediaManagementSettings({ acoustid_api_key: '' });
      onChange((prev) => (prev ? { ...prev, acoustid_api_key: updated.acoustid_api_key ?? '', fingerprint_on_weak_match: updated.fingerprint_on_weak_match } : updated));
      setKeyDraft(null);
      setConfirmRemoveKey(false);
      onToast('AcoustID key removed');
    } catch {
      onToast('Failed to remove AcoustID key', 'error');
    } finally {
      setRemovingKey(false);
    }
  };

  const handleRemoveUserKey = async (): Promise<void> => {
    setRemovingUserKey(true);
    try {
      // An explicit empty string clears the stored key server-side.
      const updated = await updateMediaManagementSettings({ acoustid_user_key: '' });
      onChange((prev) => (prev ? { ...prev, acoustid_user_key: updated.acoustid_user_key ?? '' } : updated));
      setUserKeyDraft(null);
      setConfirmRemoveUserKey(false);
      setContributions(null);
      onToast('AcoustID user key removed');
    } catch {
      onToast('Failed to remove AcoustID user key', 'error');
    } finally {
      setRemovingUserKey(false);
    }
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!settings) return;
    setIsSaving(true);
    try {
      const payload: Partial<MediaManagementSettings> = {
        fingerprint_on_weak_match: settings.fingerprint_on_weak_match,
      };
      if (!mbFromEnv) {
        payload.mb_mirror_url = settings.mb_mirror_url;
      }
      if (keyDraft !== null) {
        payload.acoustid_api_key = keyDraft;
      }
      if (userKeyDraft !== null) {
        payload.acoustid_user_key = userKeyDraft;
      }
      const updated = await updateMediaManagementSettings(payload);
      onChange(updated);
      setKeyDraft(null);
      setUserKeyDraft(null);
      onToast('Metadata sources saved');
    } catch (err) {
      onToast(err instanceof ApiError ? err.message : 'Failed to save metadata sources', 'error');
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <MachinedCard className="p-3 sm:p-6 max-w-2xl space-y-5">
      <div>
        <h4 className="text-sm font-bold uppercase tracking-wider font-mono text-white">Metadata Sources</h4>
        <p className="text-xs text-neutral-400 font-mono mt-1">
          Where TrackSeerr looks up artists, albums and tracks, and identifies files by their audio
        </p>
      </div>

      <form onSubmit={handleSave} className="space-y-5">
        <div className="space-y-3">
          <div>
            <h5 className="text-xs font-bold uppercase font-mono text-white">MusicBrainz server</h5>
            <p className="text-[11px] text-neutral-400 font-mono mt-0.5">
              Where TrackSeerr looks up artists, albums and tracks. Leave the default, or point it at your own mirror.
            </p>
          </div>
          <div>
            <label htmlFor={mbMirrorId} className={labelClass}>Server address</label>
            <div className="flex items-stretch gap-2">
              <input
                id={mbMirrorId}
                name="mb_mirror_url"
                type="text"
                autoComplete="off"
                spellCheck={false}
                disabled={mbFromEnv}
                value={settings?.mb_mirror_url ?? ''}
                onChange={(e) => {
                  const v = e.target.value;
                  onChange((prev) => (prev ? { ...prev, mb_mirror_url: v } : prev));
                }}
                placeholder="https://api.brainzmash.cc"
                className={`${inputClass} font-mono`}
              />
              <TapeDeckButton
                type="button"
                size="sm"
                disabled={isTestingMb || !(settings?.mb_mirror_url ?? '').trim()}
                onClick={async () => {
                  const value = (settings?.mb_mirror_url ?? '').trim();
                  setIsTestingMb(true);
                  try {
                    const res = await testMusicBrainzMirror(value);
                    if (res.online) {
                      onToast('MusicBrainz server is reachable');
                    } else {
                      onToast(`Could not use that server: ${res.error ?? 'no answer'}`, 'error');
                    }
                  } catch (err) {
                    onToast(err instanceof ApiError ? err.message : 'Test failed', 'error');
                  } finally {
                    setIsTestingMb(false);
                  }
                }}
                icon={isTestingMb ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
              >
                Test
              </TapeDeckButton>
            </div>
            {mbFromEnv ? (
              <p className="text-[11px] text-neutral-500 font-mono mt-1">
                Set by the MUSICBRAINZ_URL environment variable. Remove it from your container settings to change the server here.
              </p>
            ) : (
              <p className="text-[11px] text-neutral-500 font-mono mt-1">
                Example: http://192.168.1.10:5000 — adding /ws/2 is fine.
              </p>
            )}
          </div>
        </div>

        <div className="pt-2 border-t border-[#1f1f1f] space-y-3">
          <div>
            <h5 className="text-xs font-bold uppercase font-mono text-white">Audio fingerprinting</h5>
            <p className="text-[11px] text-neutral-400 font-mono mt-0.5">
              Identify downloads by their audio via AcoustID when tags are not enough.
            </p>
          </div>
          <div>
            <label htmlFor={acoustidKeyId} className={labelClass}>AcoustID API key</label>
            <div className="flex items-stretch gap-2">
              <input
                id={acoustidKeyId}
                name="acoustid_api_key"
                type={showKey ? 'text' : 'password'}
                autoComplete="off"
                spellCheck={false}
                value={keyValue}
                onChange={(e) => setKeyDraft(e.target.value)}
                onFocus={() => {
                  // Replacing a masked key discards the whole masked value, including its visible tail.
                  if (keyMasked) setKeyDraft('');
                }}
                onBlur={() => {
                  // Left empty after focusing a masked key: keep the configured key unchanged.
                  if (keyDraft === '' && isMaskedKey(serverKey)) setKeyDraft(null);
                }}
                placeholder="Application API key"
                className={`${inputClass} font-mono`}
              />
              <TapeDeckButton
                type="button"
                size="sm"
                aria-label={showKey ? 'Hide AcoustID API key' : 'Show AcoustID API key'}
                title={showKey ? 'Hide key' : 'Show key'}
                onClick={() => setShowKey((v) => !v)}
                icon={showKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              />
              {serverKey.trim().length > 0 && (
                <TapeDeckButton
                  type="button"
                  size="sm"
                  aria-label="Remove AcoustID API key"
                  title="Remove key"
                  onClick={() => setConfirmRemoveKey(true)}
                  icon={<Trash2 className="h-4 w-4" />}
                >
                  Remove key
                </TapeDeckButton>
              )}
            </div>
            <p className="text-[11px] text-neutral-500 font-mono mt-1">
              {keyMasked ? 'A key is configured; click the field to replace it. ' : ''}
              Get a free application key at acoustid.org/new-application
            </p>
          </div>
          <div>
            <label htmlFor={acoustidUserKeyId} className={labelClass}>AcoustID user key</label>
            <div className="flex items-stretch gap-2">
              <input
                id={acoustidUserKeyId}
                name="acoustid_user_key"
                type={showUserKey ? 'text' : 'password'}
                autoComplete="off"
                spellCheck={false}
                value={userKeyValue}
                onChange={(e) => setUserKeyDraft(e.target.value)}
                onFocus={() => {
                  // Replacing a masked key discards the whole masked value, including its visible tail.
                  if (userKeyMasked) setUserKeyDraft('');
                }}
                onBlur={() => {
                  // Left empty after focusing a masked key: keep the configured key unchanged.
                  if (userKeyDraft === '' && isMaskedKey(serverUserKey)) setUserKeyDraft(null);
                }}
                placeholder="User key"
                className={`${inputClass} font-mono`}
              />
              <TapeDeckButton
                type="button"
                size="sm"
                aria-label={showUserKey ? 'Hide AcoustID user key' : 'Show AcoustID user key'}
                title={showUserKey ? 'Hide key' : 'Show key'}
                onClick={() => setShowUserKey((v) => !v)}
                icon={showUserKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              />
              {serverUserKey.trim().length > 0 && (
                <TapeDeckButton
                  type="button"
                  size="sm"
                  aria-label="Remove AcoustID user key"
                  title="Remove key"
                  onClick={() => setConfirmRemoveUserKey(true)}
                  icon={<Trash2 className="h-4 w-4" />}
                >
                  Remove key
                </TapeDeckButton>
              )}
            </div>
            <p className="text-[11px] text-neutral-500 font-mono mt-1">
              {userKeyMasked ? 'A key is configured; click the field to replace it. ' : ''}
              Lets TrackSeerr submit fingerprints of files that have MusicBrainz IDs to AcoustID, credited to you. Get yours at{' '}
              <a
                href="https://acoustid.org/api-key"
                target="_blank"
                rel="noopener noreferrer"
                className="text-[var(--accent-amber)] hover:underline inline-flex items-center gap-0.5"
              >
                acoustid.org/api-key
                <ExternalLink className="h-3 w-3" aria-hidden="true" />
              </a>
              .
            </p>
            {userKeyConfigured && (
              <div className="mt-2 text-[11px] font-mono text-neutral-400 flex flex-wrap items-center gap-2">
                <span>
                  {loadingContributions
                    ? 'Loading contributions\u2026'
                    : `Submitted ${contributions?.counts?.pending ?? 0} \u00B7 imported ${contributions?.counts?.imported ?? 0} \u00B7 skipped ${contributions?.counts?.skipped ?? 0}`}
                </span>
                <span className="text-neutral-600">\u00B7</span>
                <a
                  href={routeToHash(settingsRouteFor('system', 'tasks'))}
                  className="text-[var(--accent-amber)] hover:underline inline-flex items-center gap-1"
                >
                  Run &ldquo;AcoustID Contribution&rdquo; from the Tasks page
                  <ArrowRight className="h-3 w-3" aria-hidden="true" />
                </a>
              </div>
            )}
          </div>
          <div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-xs font-mono text-neutral-300">Identify by fingerprint when tags are ambiguous</span>
              <TactileSwitch
                checked={keyConfigured && (settings?.fingerprint_on_weak_match ?? false)}
                disabled={!keyConfigured}
                onChange={(val) => onChange((prev) => (prev ? { ...prev, fingerprint_on_weak_match: val } : null))}
                label="Identify by fingerprint when tags are ambiguous"
                name="fingerprint_on_weak_match"
              />
            </div>
            <p className="text-[11px] text-neutral-500 font-mono mt-1">
              When a downloaded file&apos;s tags don&apos;t confidently match a track, look it up by audio fingerprint
              (AcoustID, ~3 lookups/sec). Off by default.
              {!keyConfigured ? ' Add an AcoustID API key to enable this.' : ''}
            </p>
          </div>
        </div>

        <ActionBar align="end" className="pt-3">
          <TapeDeckButton
            type="submit"
            variant="amber"
            size="md"
            disabled={isSaving}
            icon={isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          >
            Save
          </TapeDeckButton>
        </ActionBar>
      </form>

      <ConfirmDialog
        isOpen={confirmRemoveKey}
        title="Remove AcoustID key?"
        confirmLabel="Remove key"
        onConfirm={() => void handleRemoveKey()}
        onCancel={() => setConfirmRemoveKey(false)}
        busy={removingKey}
      >
        <p>Fingerprint identification stops until a new key is added.</p>
      </ConfirmDialog>
      <ConfirmDialog
        isOpen={confirmRemoveUserKey}
        title="Remove AcoustID user key?"
        confirmLabel="Remove user key"
        onConfirm={() => void handleRemoveUserKey()}
        onCancel={() => setConfirmRemoveUserKey(false)}
        busy={removingUserKey}
      >
        <p>TrackSeerr will stop submitting fingerprints credited to your AcoustID account.</p>
      </ConfirmDialog>
    </MachinedCard>
  );
};
