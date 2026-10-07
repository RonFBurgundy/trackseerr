import React, { useEffect } from 'react';
import { Check, AlertTriangle, Loader2 } from 'lucide-react';
import { useScrobbling } from '@/hooks/useScrobbling';
import { MusicIdentityCard } from './MusicIdentityCard';
import { RecentListens } from './RecentListens';
import { ScrobbleAdminPanel } from './ScrobbleAdminPanel';

export interface ScrobblingSettingsProps {
  isAdmin: boolean;
  /** False when no media server is connected (hides the Plex-only admin controls). */
  hasMediaServer?: boolean;
}

const SERVER_SCROBBLING_ID = 'server-scrobbling';

export const ScrobblingSettings: React.FC<ScrobblingSettingsProps> = ({ isAdmin, hasMediaServer = true }) => {
  const s = useScrobbling(isAdmin);
  const { notice, dismissNotice } = s;

  useEffect(() => {
    if (!notice) return;
    const t = window.setTimeout(dismissNotice, 6000);
    return () => window.clearTimeout(t);
  }, [notice, dismissNotice]);

  return (
    <div className="space-y-6">
      {notice && (
        <div
          role="status"
          className={`flex items-center gap-2 bg-[#161616] border rounded-[4px] px-4 py-2.5 text-xs font-mono ${
            notice.kind === 'success' ? 'border-[#22c55e] text-[#22c55e]' : 'border-[#ef4444] text-[#ef4444]'
          }`}
        >
          {notice.kind === 'success' ? <Check className="h-4 w-4" /> : <AlertTriangle className="h-4 w-4" />}
          <span className="flex-1">{notice.message}</span>
          <button type="button" onClick={dismissNotice} className="min-h-[36px] sm:min-h-0 px-2 uppercase">
            Dismiss
          </button>
        </div>
      )}

      {s.isLoading && (
        <div className="flex flex-col items-center justify-center py-16 gap-3">
          <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
          <span className="text-xs uppercase tracking-widest text-neutral-400 font-mono">Loading Scrobbling...</span>
        </div>
      )}

      {!s.isLoading && s.config && (
        <div className="grid grid-cols-1 gap-6 max-w-3xl">
          <MusicIdentityCard
            config={s.config}
            isSaving={s.isSaving}
            lastfmUnavailable={s.lastfmUnavailable}
            isAdmin={isAdmin}
            serverCardId={SERVER_SCROBBLING_ID}
            onConnectLastfm={s.connectLastfm}
            onDisconnectLastfm={s.disconnectLastfm}
            onToggleEnabled={s.setScrobblingEnabled}
            onSaveListenBrainz={s.saveListenBrainzToken}
            onUnlinkListenBrainz={s.unlinkListenBrainz}
          />
          <RecentListens listens={s.listens} />
        </div>
      )}

      {!s.isLoading && isAdmin && (
        <div id={SERVER_SCROBBLING_ID} className="max-w-3xl">
          <ScrobbleAdminPanel
            serverConfig={s.serverConfig}
            webhookUrl={s.webhookUrl}
            users={s.users}
            isSaving={s.isSaving}
            onSaveServerConfig={s.saveServerConfig}
            onRotateWebhook={s.rotateWebhook}
            onSaveUser={s.saveUserConfig}
            showPlexOptions={hasMediaServer}
          />
        </div>
      )}
    </div>
  );
};
