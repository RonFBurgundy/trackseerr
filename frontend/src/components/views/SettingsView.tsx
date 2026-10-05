import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Check, Loader2, AlertTriangle } from 'lucide-react';
import type { LibraryManagerMode } from '@/types/models';
import { ScrobblingSettings } from '@/components/scrobbling';
import { AccountPanel } from '@/components/account';
import { UsersPanel } from '@/components/admin';
import { RoleChangeBanner } from '@/components/deployment';
import { MediaServerPanel } from '@/components/mediaServer';
import {
  SettingsNav,
  InactiveGate,
  LibraryManagerSwitch,
  GeneralPanel,
  MediaFoldersPanel,
  ClientsPanel,
  IndexersPanel,
  LidarrPanel,
  ImportListsPanel,
  buildSettingsTree,
  settingsPanel,
  SELF_SERVICE_TABS,
  MEDIA_MANAGEMENT_TABS,
} from '@/components/settings';
import type { ManagedExternally } from '@/components/settings';
import { CustomFormatsPage, ProfilesPage, QualityDefinitionsPanel } from '@/components/profiles';
import { SystemPage } from '@/components/system';
import type { UseAccountReturn } from '@/hooks/useAccount';
import { settingsRouteFor } from '@/hooks/useAppRoute';
import type { SettingsRoute } from '@/hooks/useAppRoute';
import { useAdminUsers } from '@/hooks/useAdminUsers';
import { useSettingsData } from '@/hooks/useSettingsData';
import { useLibraryManager } from '@/hooks/useLibraryManager';

export type { SettingsTab } from '@/components/settings';

export interface SettingsViewProps {
  /** Current location, already clamped to what this user may see. */
  route: SettingsRoute;
  onNavigate: (route: SettingsRoute) => void;
  isAdmin?: boolean;
  showGatewayNote?: boolean;
  /** Core tier only: shows the Request portal card under System. */
  isCore?: boolean;
  accountHook: UseAccountReturn;
  currentUserId?: string | number;
  /** Local sign-in succeeded but MFA enrollment is mandatory: only the Account tab is usable. */
  mfaEnrollmentRequired?: boolean;
  /** False unless Plex is the media server: Plex-only settings are hidden. */
  hasMediaServer?: boolean;
}

interface ToastState {
  message: string;
  tone: 'ok' | 'error';
}

export const SettingsView: React.FC<SettingsViewProps> = ({
  route,
  onNavigate,
  isAdmin = false,
  showGatewayNote = false,
  isCore = false,
  accountHook,
  currentUserId,
  mfaEnrollmentRequired = false,
  hasMediaServer = true,
}) => {
  const activeTab = settingsPanel(route);
  const adminUsersHook = useAdminUsers(isAdmin && activeTab === 'users' && !mfaEnrollmentRequired);
  const data = useSettingsData(isAdmin);
  const libraryManager = useLibraryManager(isAdmin && !mfaEnrollmentRequired);
  const [pendingMode, setPendingMode] = useState<LibraryManagerMode | null>(null);
  const [toast, setToast] = useState<ToastState | null>(null);
  const toastTimer = useRef<number | null>(null);

  const showToast = useCallback((message: string, tone: 'ok' | 'error' = 'ok') => {
    setToast({ message, tone });
    if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), tone === 'error' ? 5000 : 3000);
  }, []);

  useEffect(
    () => () => {
      if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    },
    []
  );

  // Server value wins; fall back to the media-settings field when the library-manager endpoint is unavailable.
  const mode: LibraryManagerMode =
    libraryManager.state?.mode ?? (data.media?.library_mode === 'lidarr' ? 'lidarr' : 'native');

  const sections = buildSettingsTree(isAdmin, mfaEnrollmentRequired);
  const inactiveIds = new Set<string>();
  if (isAdmin && !mfaEnrollmentRequired) {
    if (mode === 'lidarr') MEDIA_MANAGEMENT_TABS.forEach((t) => inactiveIds.add(t));
    else inactiveIds.add('lidarr');
  }

  const requestSwitch = (target: LibraryManagerMode) => {
    setPendingMode(target);
    onNavigate(settingsRouteFor('general'));
  };

  const lidarrUrl = data.lidarr?.url || data.general?.lidarr_url || null;
  const managedByLidarr: ManagedExternally = {
    url: lidarrUrl,
    onOpenLidarrSettings: () => onNavigate(settingsRouteFor('lidarr')),
  };

  const isSelfServiceTab = SELF_SERVICE_TABS.has(activeTab);
  const mediaActive = mode === 'native';

  return (
    <div className="space-y-6">
      {toast && (
        <div
          role="status"
          className={`fixed top-20 right-4 left-4 sm:left-auto z-50 bg-[#161616] border px-4 py-2.5 rounded-[4px] text-xs font-mono shadow-lg flex items-center gap-2 ${
            toast.tone === 'error' ? 'border-red-700 text-red-300' : 'border-[#e5a00d] text-[#e5a00d]'
          }`}
        >
          {toast.tone === 'error' ? <AlertTriangle className="h-4 w-4 shrink-0" /> : <Check className="h-4 w-4 shrink-0" />}
          <span>{toast.message}</span>
        </div>
      )}

      <RoleChangeBanner enabled={isAdmin && !mfaEnrollmentRequired} />

      {showGatewayNote && (
        <div className="bg-[#121212] border border-[#2a2a2a] rounded-[4px] px-3 py-2 sm:px-4 sm:py-3 text-xs font-mono text-neutral-400">
          Admin settings are available on the TrackSeerr Core admin interface.
        </div>
      )}

      <SettingsNav sections={sections} route={route} onNavigate={onNavigate} inactiveIds={inactiveIds} />

      {activeTab === 'scrobbling' && !mfaEnrollmentRequired && <ScrobblingSettings isAdmin={isAdmin} hasMediaServer={hasMediaServer} />}

      {activeTab === 'account' && (
        <AccountPanel accountHook={accountHook} isAdmin={isAdmin} enrollmentBlocking={mfaEnrollmentRequired} />
      )}

      {activeTab === 'users' && isAdmin && !mfaEnrollmentRequired && (
        <UsersPanel adminHook={adminUsersHook} currentUserId={currentUserId} />
      )}

      {activeTab === 'import-lists' && isAdmin && !mfaEnrollmentRequired && <ImportListsPanel libraryMode={mode} onToast={showToast} />}

      {activeTab === 'media-server' && isAdmin && !mfaEnrollmentRequired && <MediaServerPanel onToast={showToast} />}

      {activeTab === 'system' && isAdmin && !mfaEnrollmentRequired && (
        <SystemPage tab={route.sub === 'system' ? route.leaf : 'status'} isCore={isCore} libraryMode={mode} onToast={showToast} />
      )}

      {data.isLoading && !isSelfServiceTab && (
        <div className="flex flex-col items-center justify-center py-16 gap-3">
          <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
          <span className="text-xs uppercase tracking-widest text-neutral-400 font-mono">
            Reading System Configuration...
          </span>
        </div>
      )}

      {!data.isLoading && activeTab === 'general' && isAdmin && (
        <div className="space-y-6">
          <LibraryManagerSwitch
            manager={libraryManager}
            mode={mode}
            pendingMode={pendingMode}
            onPendingChange={setPendingMode}
            onToast={showToast}
          />
          <GeneralPanel settings={data.general} onChange={data.setGeneral} onToast={showToast} />
        </div>
      )}

      {!data.isLoading && activeTab === 'media' && isAdmin && (
        <InactiveGate active={mediaActive} activeManager={mode} onRequestSwitch={requestSwitch}>
          <MediaFoldersPanel settings={data.media} onChange={data.setMedia} onToast={showToast} />
        </InactiveGate>
      )}

      {!data.isLoading && activeTab === 'quality' && isAdmin && (
        <InactiveGate
          active={mediaActive}
          activeManager={mode}
          onRequestSwitch={requestSwitch}
          managedExternally={managedByLidarr}
        >
          <QualityDefinitionsPanel enabled={mediaActive} onToast={showToast} />
        </InactiveGate>
      )}

      {!data.isLoading && activeTab === 'profiles' && isAdmin && (
        <InactiveGate
          active={mediaActive}
          activeManager={mode}
          onRequestSwitch={requestSwitch}
          managedExternally={managedByLidarr}
        >
          <ProfilesPage
            enabled={mediaActive}
            media={data.media}
            onMediaChange={data.setMedia}
            indexers={data.indexers}
            onToast={showToast}
          />
        </InactiveGate>
      )}

      {!data.isLoading && activeTab === 'custom-formats' && isAdmin && (
        <InactiveGate
          active={mediaActive}
          activeManager={mode}
          onRequestSwitch={requestSwitch}
          managedExternally={managedByLidarr}
        >
          <CustomFormatsPage enabled={mediaActive} onToast={showToast} />
        </InactiveGate>
      )}

      {!data.isLoading && activeTab === 'clients' && isAdmin && (
        <InactiveGate
          active={mediaActive}
          activeManager={mode}
          onRequestSwitch={requestSwitch}
          managedExternally={managedByLidarr}
        >
          <ClientsPanel clients={data.clients} reload={data.reload} onToast={showToast} />
        </InactiveGate>
      )}

      {!data.isLoading && activeTab === 'indexers' && isAdmin && (
        <InactiveGate
          active={mediaActive}
          activeManager={mode}
          onRequestSwitch={requestSwitch}
          managedExternally={managedByLidarr}
        >
          <IndexersPanel indexers={data.indexers} reload={data.reload} onToast={showToast} />
        </InactiveGate>
      )}

      {!data.isLoading && activeTab === 'lidarr' && isAdmin && (
        <LidarrPanel
          settings={data.lidarr}
          onChange={data.setLidarr}
          isActive={mode === 'lidarr'}
          activeManager={mode}
          onRequestSwitch={requestSwitch}
          onToast={showToast}
        />
      )}
    </div>
  );
};
