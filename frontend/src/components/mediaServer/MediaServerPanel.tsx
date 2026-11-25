import React, { useCallback, useEffect, useState } from 'react';
import { ExternalLink, Loader2, LogIn, LogOut, X } from 'lucide-react';
import { MachinedCard, PageLoader, TapeDeckButton } from '@/components/ui';
import { errorMessage } from '@/services/apiClient';
import { disconnectPlexAccount } from '@/services/mediaServerService';
import { useMediaServerSettings } from '@/hooks/useMediaServerSettings';
import { usePlexAccountSignIn } from '@/hooks/usePlexAccountSignIn';
import type { MediaServerItem } from '@/types/mediaServer';
import { MediaServerList } from './MediaServerList';

export interface MediaServerPanelProps {
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Settings > Media Server: list and configure media servers plus the Plex account sign-in. */
export const MediaServerPanel: React.FC<MediaServerPanelProps> = ({ onToast }) => {
  const hook = useMediaServerSettings(true);
  const { settings, reload } = hook;
  const [servers, setServers] = useState<MediaServerItem[]>([]);
  const [isDisconnecting, setIsDisconnecting] = useState<boolean>(false);

  const handlePlexConnected = useCallback(
    (username: string) => {
      onToast(username ? `Signed in to Plex as ${username}` : 'Signed in to Plex', 'ok');
      void reload();
    },
    [onToast, reload]
  );

  const plexSignIn = usePlexAccountSignIn(handlePlexConnected);

  useEffect(() => {
    if (plexSignIn.error) {
      onToast(plexSignIn.error, 'error');
    }
  }, [plexSignIn.error, onToast]);

  if (hook.isLoading) {
    return <PageLoader message="Reading media server settings" />;
  }

  const handlePlexDisconnect = async () => {
    setIsDisconnecting(true);
    try {
      await disconnectPlexAccount();
      await reload();
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to sign out of Plex'), 'error');
    } finally {
      setIsDisconnecting(false);
    }
  };

  const hasPlex = servers.some((s) => s.kind === 'plex');
  const isWaitingForPlex = plexSignIn.phase === 'waiting' || plexSignIn.phase === 'blocked';

  return (
    <div className="space-y-4">
      {hook.error && (
        <p role="alert" className="text-xs font-mono text-red-300">
          {hook.error}
        </p>
      )}

      <MachinedCard className="p-3 sm:p-6 space-y-5">
        <div className="border-b border-[#222222] pb-3">
          <h4 className="text-sm font-bold uppercase font-mono text-white">Media Servers</h4>
          <p className="text-xs text-neutral-400 font-mono mt-0.5">
            Playlists are pushed to every server that has Push playlists on, and each one is refreshed after imports.
          </p>
        </div>

        {hasPlex && (
          <div className="rounded-[4px] border border-[#222222] bg-[#0f0f0f] p-3 sm:p-4 space-y-3">
            <div className="space-y-1">
              <h5 className="text-xs font-bold uppercase font-mono text-white">Plex account</h5>
              {settings?.plex_account_connected ? (
                <p className="text-xs font-mono text-neutral-300">
                  Signed in as <span className="text-white font-semibold">{settings.plex_account_username}</span>.
                  TrackSeerr uses this account for Plex Home users and other users&apos; playlists.
                </p>
              ) : (
                <p className="text-xs font-mono text-neutral-400">
                  Sign in as the Plex server owner so TrackSeerr can reach Plex Home users and other users&apos;
                  playlists safely.
                </p>
              )}
            </div>

            {settings?.plex_account_connected ? (
              <div className="flex flex-wrap items-center gap-2">
                <TapeDeckButton
                  type="button"
                  size="sm"
                  className="w-full sm:w-auto"
                  aria-label="Sign out of Plex account"
                  disabled={isDisconnecting}
                  onClick={() => void handlePlexDisconnect()}
                  icon={
                    isDisconnecting ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <LogOut className="h-3.5 w-3.5" />
                    )
                  }
                >
                  Sign out
                </TapeDeckButton>
              </div>
            ) : isWaitingForPlex ? (
              <div className="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-3">
                <div className="flex items-center gap-2 text-xs font-mono text-neutral-300">
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-[#e5a00d] shrink-0" aria-hidden="true" />
                  <span>Waiting for Plex sign-in…</span>
                </div>
                {plexSignIn.phase === 'blocked' && plexSignIn.authUrl && (
                  <a
                    href={plexSignIn.authUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 text-xs font-mono text-[#e5a00d] underline hover:text-[#f5b82e]"
                  >
                    <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    <span>Open Plex sign-in</span>
                  </a>
                )}
                <TapeDeckButton
                  type="button"
                  size="sm"
                  className="w-full sm:w-auto sm:ml-auto"
                  aria-label="Cancel Plex sign-in"
                  onClick={plexSignIn.cancel}
                  icon={<X className="h-3.5 w-3.5" />}
                >
                  Cancel
                </TapeDeckButton>
              </div>
            ) : (
              <div className="flex flex-wrap items-center gap-2">
                <TapeDeckButton
                  type="button"
                  size="sm"
                  active
                  className="w-full sm:w-auto"
                  aria-label="Sign in with Plex"
                  onClick={() => void plexSignIn.start()}
                  icon={<LogIn className="h-3.5 w-3.5" />}
                >
                  Sign in with Plex
                </TapeDeckButton>
              </div>
            )}
          </div>
        )}

        <MediaServerList onToast={onToast} onServersChange={setServers} />
      </MachinedCard>
    </div>
  );
};
