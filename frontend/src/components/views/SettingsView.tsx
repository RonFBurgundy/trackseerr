import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Check, AlertTriangle } from 'lucide-react';
import { CassetteLoader } from '@/components/ui';
import type { LibraryManagerMode } from '@/types/models';
import { ScrobblingSettings } from '@/components/scrobbling';
import { AccountPanel } from '@/components/account';
import { UsersPanel } from '@/components/admin';
import { RoleChangeBanner } from '@/components/deployment';
import { MediaServerPanel } from '@/components/mediaServer';
import { NotificationsPanel } from '@/components/notifications';
import {
  SettingsNav,
  InactiveGate,
  LibraryManagerSwitch,
  ModeSwitchConfirm,
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
import { SystemPage, systemTabOwnsScroll } from '@/components/system';
import { PageFrame, RefreshBinding } from '@/components/layout';
import type { UseAccountReturn } from '@/hooks/useAccount';
import type { UseUpdateCheckReturn } from '@/hooks';
import { settingsRouteFor } from '@/hooks/useAppRoute';
import type { SettingsRoute } from '@/hooks/useAppRoute';
import { useAdminUsers } from '@/hooks/useAdminUsers';
import { useSettingsData } from '@/hooks/useSettingsData';
import { useLibraryManager } from '@/hooks/useLibraryManager';
import { useLibraryModeSwitch } from '@/hooks/useLibraryModeSwitch';

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
  updateCheck?: UseUpdateCheckReturn;
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
  updateCheck,
}) => {
  const activeTab = settingsPanel(route);
  const adminUsersHook = useAdminUsers(isAdmin && activeTab === 'users' && !mfaEnrollmentRequired);
  const data = useSettingsData(isAdmin);
  const libraryManager = useLibraryManager(isAdmin && !mfaEnrollmentRequired);
  const [toast, setToast] = useState<ToastState | null>(null);
  const toastTimer = useRef<number | null>(null);

  const showToast = useCallback((message: string, tone: 'ok' | 'error' = 'ok') => {
    setToast({ message, tone });
    if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), tone === 'error' ? 5000 : 3000);
  }, []);

  const modeSwitch = useLibraryModeSwitch(libraryManager, showToast);
  const { request: requestSwitch } = modeSwitch;

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

  // A saved, working Lidarr connection: refresh the server's "configured" flag, then offer the switch in place.
  const handleLidarrVerified = async () => {
    await libraryManager.refresh();
    requestSwitch('lidarr');
  };

  const lidarrUrl = data.lidarr?.url || null;
  const managedByLidarr: ManagedExternally = {
    url: lidarrUrl,
    onOpenLidarrSettings: () => onNavigate(settingsRouteFor('lidarr')),
  };

  const isSelfServiceTab = SELF_SERVICE_TABS.has(activeTab);
  const mediaActive = mode === 'native';

  const systemLeaf = route.sub === 'system' ? route.leaf : 'status';
  // System events/logs own their scroller, so the settings body must not scroll for them (one scroll region per column).
  const bodyScrolls = !(activeTab === 'system' && isAdmin && !mfaEnrollmentRequired && systemTabOwnsScroll(systemLeaf));

  return (
    <PageFrame
      scroll={bodyScrolls}
      bodyClassName="space-y-6"
      nav={
      <>
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

      <SettingsNav
        sections={sections}
        route={route}
        onNavigate={onNavigate}
        inactiveIds={inactiveIds}
        updateAvailable={updateCheck?.updateAvailable}
      />
      </>
      }
    >
      {isAdmin && !mfaEnrollmentRequired && !isSelfServiceTab && <RefreshBinding onRefresh={data.reload} />}

      {/* Informational banners scroll away with the content; pages whose child owns the scroller skip them. */}
      {bodyScrolls && <RoleChangeBanner enabled={isAdmin && !mfaEnrollmentRequired} />}

      {bodyScrolls && showGatewayNote && (
        <div
          title="Admin settings are available on the TrackSeerr Core admin interface."
          className="bg-[#121212] border border-[#2a2a2a] rounded-[4px] px-3 py-2 text-xs font-mono text-neutral-400 truncate"
        >
          Admin settings are available on the TrackSeerr Core admin interface.
        </div>
      )}

      {isAdmin && !mfaEnrollmentRequired && modeSwitch.pendingMode && (
        <ModeSwitchConfirm
          mode={mode}
          pendingMode={modeSwitch.pendingMode}
          state={libraryManager.state}
          isSwitching={libraryManager.isSwitching}
          onCancel={modeSwitch.cancel}
          onConfirm={() => void modeSwitch.confirm()}
        />
      )}

      {activeTab === 'scrobbling' && !mfaEnrollmentRequired && <ScrobblingSettings isAdmin={isAdmin} hasMediaServer={hasMediaServer} />}

      {activeTab === 'account' && (
        <AccountPanel accountHook={accountHook} isAdmin={isAdmin} enrollmentBlocking={mfaEnrollmentRequired} onToast={showToast} />
      )}

      {activeTab === 'users' && isAdmin && !mfaEnrollmentRequired && (
        <UsersPanel adminHook={adminUsersHook} currentUserId={currentUserId} />
      )}

      {activeTab === 'import-lists' && isAdmin && !mfaEnrollmentRequired && <ImportListsPanel libraryMode={mode} onToast={showToast} />}

      {activeTab === 'notifications' && isAdmin && !mfaEnrollmentRequired && <NotificationsPanel onToast={showToast} />}

      {activeTab === 'media-server' && isAdmin && !mfaEnrollmentRequired && <MediaServerPanel onToast={showToast} />}

      {activeTab === 'system' && isAdmin && !mfaEnrollmentRequired && (
        <SystemPage tab={systemLeaf} isCore={isCore} libraryMode={mode} onToast={showToast} updateCheck={updateCheck} />
      )}

      {data.isLoading && !isSelfServiceTab && (
        <div className="py-16">
          <CassetteLoader size="md" />
        </div>
      )}

      {!data.isLoading && activeTab === 'general' && isAdmin && (
        <div className="space-y-6">
          <LibraryManagerSwitch
            manager={libraryManager}
            mode={mode}
            onRequestSwitch={requestSwitch}
          />
          <GeneralPanel
            settings={data.general}
            onChange={data.setGeneral}
            onToast={showToast}
            isAdmin={isAdmin}
            isGateway={Boolean(showGatewayNote)}
          />
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
            libraryMode={mode}
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
          <IndexersPanel indexers={data.indexers} media={data.media} reload={data.reload} onToast={showToast} />
        </InactiveGate>
      )}

      {!data.isLoading && activeTab === 'lidarr' && isAdmin && (
        <LidarrPanel
          settings={data.lidarr}
          onChange={data.setLidarr}
          isActive={mode === 'lidarr'}
          activeManager={mode}
          onRequestSwitch={requestSwitch}
          canSwitchToLidarr={libraryManager.state?.lidarr_configured ?? false}
          onCredentialsVerified={handleLidarrVerified}
          onToast={showToast}
        />
      )}
    </PageFrame>
  );
};
