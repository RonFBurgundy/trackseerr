import React, { useState } from 'react';
import { Loader2, Radio } from 'lucide-react';
import type { ScrobbleConfig } from '@/types/models';
import { MachinedCard, TapeDeckButton, TactileSwitch } from '@/components/ui';

export interface MusicIdentityCardProps {
  config: ScrobbleConfig;
  isSaving: boolean;
  lastfmUnavailable: boolean;
  onConnectLastfm: () => Promise<void>;
  onDisconnectLastfm: () => Promise<void>;
  onToggleEnabled: (enabled: boolean) => Promise<void>;
  onSaveListenBrainz: (token: string) => Promise<boolean>;
  onUnlinkListenBrainz: () => Promise<void>;
}

const inputClass =
  'w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]';

export const MusicIdentityCard: React.FC<MusicIdentityCardProps> = ({
  config,
  isSaving,
  lastfmUnavailable,
  onConnectLastfm,
  onDisconnectLastfm,
  onToggleEnabled,
  onSaveListenBrainz,
  onUnlinkListenBrainz,
}) => {
  const [lbOpen, setLbOpen] = useState<boolean>(config.listenbrainz_connected);
  const [lbToken, setLbToken] = useState<string>('');

  const handleSaveLb = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!lbToken.trim()) return;
    if (await onSaveListenBrainz(lbToken)) setLbToken('');
  };

  const active = config.scrobbling_enabled && (config.lastfm_connected || config.listenbrainz_connected);

  return (
    <MachinedCard className="p-4 sm:p-6 space-y-6">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-bold uppercase tracking-wider text-white">Music Identity</h3>
        {active && (
          <span className="inline-flex items-center gap-2 text-xs font-mono uppercase text-[#22c55e]">
            <span className="h-2 w-2 rounded-full bg-[#22c55e] shadow-[0_0_6px_rgba(34,197,94,0.7)]" />
            Scrobbling Active
          </span>
        )}
      </div>

      {/* Last.fm */}
      <div className="space-y-3">
        <div className="text-xs uppercase font-mono tracking-wider text-neutral-400">Last.fm</div>
        {config.lastfm_connected ? (
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div className="text-sm text-white">
              Connected as <span className="font-mono text-[#e5a00d]">{config.lastfm_username ?? 'unknown'}</span>
            </div>
            <TapeDeckButton size="sm" variant="danger" disabled={isSaving} onClick={() => void onDisconnectLastfm()}>
              Disconnect
            </TapeDeckButton>
          </div>
        ) : lastfmUnavailable ? (
          <p className="text-sm text-neutral-400">Your server admin hasn&apos;t enabled Last.fm yet</p>
        ) : (
          <div className="space-y-2">
            <TapeDeckButton
              variant="amber"
              disabled={isSaving}
              onClick={() => void onConnectLastfm()}
              icon={isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Radio className="h-4 w-4" />}
              className="w-full sm:w-auto"
            >
              Connect Last.fm
            </TapeDeckButton>
            <p className="text-xs text-neutral-400">
              <a
                href="https://www.last.fm/join"
                target="_blank"
                rel="noopener noreferrer"
                className="text-[#e5a00d] hover:underline"
              >
                Don&apos;t have a Last.fm account? Create one in 30 seconds
              </a>
            </p>
          </div>
        )}
      </div>

      {/* ListenBrainz accordion */}
      <div className="border-t border-[#222222] pt-4 space-y-3">
        <button
          type="button"
          aria-expanded={lbOpen}
          onClick={() => setLbOpen((v) => !v)}
          className="w-full min-h-[44px] flex items-center justify-between text-left text-xs uppercase font-mono tracking-wider text-neutral-400 hover:text-white"
        >
          <span>ListenBrainz</span>
          <span className="text-[#e5a00d]">{lbOpen ? '−' : '+'}</span>
        </button>
        {lbOpen && (
          <div className="space-y-3">
            {config.listenbrainz_connected && (
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-sm text-white">
                <span>
                  Connected as{' '}
                  <span className="font-mono text-[#e5a00d]">{config.listenbrainz_username ?? 'unknown'}</span>
                </span>
                <TapeDeckButton size="sm" variant="danger" disabled={isSaving} onClick={() => void onUnlinkListenBrainz()}>
                  Disconnect
                </TapeDeckButton>
              </div>
            )}
            <form onSubmit={handleSaveLb} className="flex flex-col sm:flex-row gap-2">
              <input
                id="music-identity-lb-token"
                name="listenbrainz-token"
                aria-label="ListenBrainz user token"
                type="password"
                autoComplete="off"
                value={lbToken}
                onChange={(e) => setLbToken(e.target.value)}
                placeholder="ListenBrainz user token"
                className={inputClass}
              />
              <TapeDeckButton type="submit" size="sm" disabled={isSaving || !lbToken.trim()}>
                Save
              </TapeDeckButton>
            </form>
          </div>
        )}
      </div>

      <div className="border-t border-[#222222] pt-4">
        <TactileSwitch
          checked={config.scrobbling_enabled}
          disabled={isSaving}
          onChange={(v) => void onToggleEnabled(v)}
          label="Scrobbling enabled"
        />
      </div>
    </MachinedCard>
  );
};
