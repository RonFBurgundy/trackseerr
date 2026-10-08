import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { Loader2, LogIn } from 'lucide-react';
import type { ArtistDiscographyAlbum, DiscoveryItem, Playlist, User } from '@/types/models';
import type { ListMonitorMode } from '@/types/importLists';
import {
  useAuth,
  useAudioPlayer,
  useDiscovery,
  useRequests,
  useIssues,
  useIssueCounts,
  useLibrary,
  useLibraryHealthCount,
  useAccount,
  useCanAutoRequestPlaylists,
  useLocalLogin,
  useDeploymentIdentity,
  useMediaServer,
  useStartupStatus,
  useAppRoute,
  useSearchShortcut,
  useSettingHighlight,
  defaultRoute,
  settingsRouteFor,
  RefreshContext,
  useRefreshRegistry,
  usePullToRefresh,
} from '@/hooks';
import type { AppRoute, MainTab, NavigateOptions } from '@/hooks';
import {
  Header,
  NavHub,
  PageFrame,
  PullRefreshIndicator,
  RefreshBinding,
  gateRoute,
  routesEqual,
  AudioPlayerBar,
  DiscoverView,
  RequestsView,
  LibraryView,
  PlaylistsView,
  ActivityView,
  WantedView,
  SettingsView,
  ObsidianModal,
  TapeDeckButton,
  MachinedCard,
  InvitePage,
  LocalLoginForm,
  CassetteLoader,
  StartupScreen,
} from '@/components';
import {
  getPlaylists,
  toggleUserTarget,
  togglePlaylistActive,
  updatePlaylistSettings,
  deletePlaylist,
  triggerSync,
  importPlaylist,
  type ImportPlaylistPayload,
} from '@/services/playlistService';
import { apiRequest } from '@/services/apiClient';
import { setPlaylistAutoRequest } from '@/services/listeningService';
import { createDiscographyRequest, MAX_BATCH_ITEMS } from '@/services/requestService';
import { peekPendingImport } from '@/services/bookmarkletImport';

/** Manual path routing: only /invite/:token is a distinct page; everything else is the SPA shell. */
export function parseInviteToken(pathname: string): string | null {
  const match = /^\/invite\/([^/]+)\/?$/.exec(pathname);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch (err: unknown) {
    if (err instanceof URIError) return null;
    throw err;
  }
}

const MainApp: React.FC = () => {
  const auth = useAuth();
  const identity = useDeploymentIdentity(auth.tier, auth.isAuthenticated);
  const mediaServer = useMediaServer();
  const accountHook = useAccount(auth.isAuthenticated);
  const canAutoRequestPlaylists = useCanAutoRequestPlaylists(auth.isAuthenticated);
  const localLogin = useLocalLogin({ onSignedIn: auth.completeLocalSignIn });
  const [showLocalLogin, setShowLocalLogin] = useState<boolean>(false);
  // Mandatory MFA enrollment: the server returns 403 for everything but /api/account*,
  // so confine the UI to Settings -> Account until it is done.
  const mfaEnrollmentRequired =
    auth.isAuthenticated &&
    (auth.mfaEnrollmentRequired ||
      Boolean(
        accountHook.account &&
          accountHook.account.auth_type === 'local' &&
          accountHook.account.mfa_required &&
          !accountHook.account.mfa_enabled
      ));
  const audioPlayer = useAudioPlayer();
  const discovery = useDiscovery(auth.isAuthenticated);
  const requestsHook = useRequests(auth.isAuthenticated);
  const issuesHook = useIssues(auth.user?.id, auth.isAuthenticated);
  const issueCounts = useIssueCounts(auth.isAuthenticated && !mfaEnrollmentRequired, auth.canUseAdminUi);
  const libraryHook = useLibrary(auth.canUseAdminUi);
  const reviewHealth = useLibraryHealthCount(auth.canUseAdminUi);

  const { route, navigate, navigateUp } = useAppRoute();
  useSearchShortcut();
  // Gate the requested location: MFA enrollment confines to Settings > Account, admin-only tabs fall back to
  // Discover, and hidden settings pages fall back to the first visible one.
  const activeRoute: AppRoute = useMemo(
    () => gateRoute(route, { isAdmin: auth.canUseAdminUi, mfaEnrollmentRequired }),
    [route, auth.canUseAdminUi, mfaEnrollmentRequired]
  );
  const activeTab: MainTab = activeRoute.tab;
  const [isAuthModalOpen, setIsAuthModalOpen] = useState<boolean>(false);
  const [isMenuOpen, setIsMenuOpen] = useState<boolean>(false);
  const closeMenu = useCallback(() => setIsMenuOpen(false), []);
  const mainRef = useRef<HTMLElement | null>(null);
  const { highlight } = useSettingHighlight();
  const refreshRegistry = useRefreshRegistry();
  const pull = usePullToRefresh({
    rootRef: mainRef,
    enabled: auth.isAuthenticated,
    onRefresh: async () => {
      // A view that registered nothing gets a full reload.
      if (!(await refreshRegistry.run())) window.location.reload();
    },
  });

  // Playlists & users data
  const [playlists, setPlaylists] = useState<Playlist[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [isPlaylistsLoading, setIsPlaylistsLoading] = useState<boolean>(false);

  // Set of requested IDs to update UI state
  const [requestedIds, setRequestedIds] = useState<Set<string>>(new Set());

  const loadPlaylistsAndUsers = useCallback(async () => {
    setIsPlaylistsLoading(true);
    try {
      // /api/users (list) is admin-only; non-admins only ever see themselves as a target.
      const [plData, usrData] = await Promise.all([
        getPlaylists().catch(() => []),
        auth.canUseAdminUi
          ? apiRequest<User[]>('/api/users').catch(() => [] as User[])
          : Promise.resolve(auth.user ? [auth.user] : ([] as User[])),
      ]);
      setPlaylists(plData);
      setUsers(usrData);
    } finally {
      setIsPlaylistsLoading(false);
    }
  }, [auth.canUseAdminUi, auth.user]);

  const handleNavigate = useCallback(
    (next: AppRoute, options?: NavigateOptions) => {
      navigate(next, options);
      // main never scrolls; the page body inside it does.
      if (!options?.replace) {
        mainRef.current?.querySelector<HTMLElement>('[data-page-body]')?.scrollTo({ top: 0, behavior: 'smooth' });
      }
    },
    [navigate]
  );

  useEffect(() => {
    if (auth.isAuthenticated) {
      setIsAuthModalOpen(false);
      loadPlaylistsAndUsers();
      if (peekPendingImport()) {
        handleNavigate({ tab: 'playlists' }, { replace: true });
      }
    }
  }, [auth.isAuthenticated, loadPlaylistsAndUsers, handleNavigate]);

  // Header keys jump to a tab's landing page; the tab already open keeps its sub-page.
  const handleTabChange = (tab: MainTab) => {
    if (tab !== activeTab) handleNavigate(defaultRoute(tab));
  };

  // Persist the gated location once signed in, so the URL always names the page actually shown.
  useEffect(() => {
    if (auth.isAuthenticated && !routesEqual(activeRoute, route)) navigate(activeRoute, { replace: true });
  }, [auth.isAuthenticated, activeRoute, route, navigate]);

  // Request an item from discovery
  const handleRequestItem = async (item: DiscoveryItem) => {
    await requestsHook.submitRequest({
      id: item.id,
      preview_url: item.preview_url ?? undefined,
      release_date: item.release_date ?? undefined,
      title: item.title,
      artist: item.artist ?? '',
      album: item.album ?? undefined,
      cover_url: item.cover_url ?? undefined,
      type: item.type,
    });
    setRequestedIds((prev) => new Set([...prev, item.id]));
    void accountHook.refresh();
  };

  // Discography batch: the albums fill one discography quota unit.
  const handleRequestDiscography = async (artist: string, albums: ArtistDiscographyAlbum[]) => {
    await createDiscographyRequest({
      kind: 'discography',
      artist,
      requests: albums.slice(0, MAX_BATCH_ITEMS).map((a) => ({
        item_type: 'album' as const,
        title: a.title,
        artist: a.artist || artist,
        album: a.title,
        cover_url: a.cover_url,
        release_date: a.release_date,
        foreign_id: a.id,
      })),
    });
    setRequestedIds((prev) => new Set([...prev, ...albums.map((a) => a.id)]));
    void requestsHook.refresh();
    void accountHook.refresh();
  };

  const handleSyncPlaylists = async () => {
    await triggerSync();
    await loadPlaylistsAndUsers();
  };

  const handleToggleTarget = async (playlistId: number | string, userIds: string[]) => {
    await toggleUserTarget(playlistId, userIds);
    await loadPlaylistsAndUsers();
  };

  const handleTogglePlaylistActive = async (playlistId: number | string, isEnabled: boolean) => {
    await togglePlaylistActive(playlistId, isEnabled);
    await loadPlaylistsAndUsers();
  };

  const handleSetPlaylistMonitorMode = async (playlist: Playlist, mode: ListMonitorMode) => {
    await updatePlaylistSettings(playlist.id, { enabled: playlist.enabled, monitor_mode: mode });
    await loadPlaylistsAndUsers();
  };

  const handleSetPlaylistAutoRequest = async (playlist: Playlist, autoRequest: boolean) => {
    await setPlaylistAutoRequest(playlist.id, autoRequest);
    await loadPlaylistsAndUsers();
  };

  const handleDeletePlaylist = async (playlistId: number | string) => {
    await deletePlaylist(playlistId);
    await loadPlaylistsAndUsers();
  };

  const handleImportPlaylist = async (payload: ImportPlaylistPayload) => {
    await importPlaylist(payload);
    await loadPlaylistsAndUsers();
  };

  return (
    <RefreshContext.Provider value={refreshRegistry}>
    <div
      className="h-screen w-screen flex flex-col overflow-hidden bg-[#0a0a0a] text-white selection:bg-[#e5a00d] selection:text-black"
      style={{ height: '100dvh' }}
    >
      {/* Sticky Header with integrated navigation */}
      <Header
        user={auth.user}
        quota={requestsHook.quota}
        activeTab={activeTab}
        onTabChange={handleTabChange}
        isAdmin={auth.canUseAdminUi}
        reviewCount={reviewHealth.count}
        issuesOpenCount={issueCounts.open}
        issuesUnreadCount={issueCounts.unread}
        tier={identity.tier}
        activityEnabled={auth.canUseAdminUi && !mfaEnrollmentRequired}
        onOpenTasks={() => handleNavigate(settingsRouteFor('system', 'tasks'))}
        onLogin={() => {
          if (mediaServer.isPlex) setIsAuthModalOpen(true);
          else setShowLocalLogin(true);
        }}
        onLogout={auth.logout}
        isMenuOpen={isMenuOpen}
        onToggleMenu={() => setIsMenuOpen((prev) => !prev)}
      />

      {/* Hub navigator: full-screen drawer on phones, side panel on desktop */}
      <NavHub
        isOpen={isMenuOpen}
        onClose={closeMenu}
        route={activeRoute}
        onNavigate={handleNavigate}
        user={auth.user}
        quota={requestsHook.quota}
        isAdmin={auth.canUseAdminUi}
        mfaEnrollmentRequired={mfaEnrollmentRequired}
        reviewCount={reviewHealth.count}
        issuesOpenCount={issueCounts.open}
        issuesUnreadCount={issueCounts.unread}
        tier={identity.tier}
        onLogout={auth.logout}
        onHighlight={highlight}
      />

      {/* Main Content Area - Locked scrolling inside container */}
      <main
        ref={mainRef}
        className="relative flex flex-col flex-1 min-h-0 overflow-hidden px-4 sm:px-6 pt-4"
        // Bottom room: the safe-area inset, plus the audio bar while a preview is loaded. Lists size to this edge.
        style={{ paddingBottom: `calc(${audioPlayer.currentTrack ? '6.5rem' : '1rem'} + env(safe-area-inset-bottom, 0px))` }}
      >
        <PullRefreshIndicator {...pull} />
        {auth.isLoading ? (
          <PageFrame bodyClassName="flex">
            <div className="m-auto py-8">
              <CassetteLoader size="lg" />
            </div>
          </PageFrame>
        ) : !auth.isAuthenticated ? (
          /* Landing Hero for Unauthenticated Visitors */
          <PageFrame bodyClassName="flex">
          <div className="m-auto p-4 relative flex items-center justify-center w-full">
            <div className="absolute w-72 h-72 bg-[#e5a00d]/10 rounded-full blur-3xl pointer-events-none" />
            <MachinedCard className="max-w-md w-full p-8 text-center space-y-6 border-[#262626] bg-[#121212] relative z-10 shadow-2xl">
              <div className="w-20 h-20 rounded-[4px] bg-[#141414] border border-[#262626] flex items-center justify-center mx-auto shadow-xl">
                <img
                  src="/trackseerr-logo.svg"
                  alt="TrackSeerr"
                  className="w-16 h-16 object-contain"
                  onError={(e) => {
                    (e.currentTarget as HTMLImageElement).src = '/static/trackseerr-logo.svg';
                  }}
                />
              </div>
              <div className="space-y-1">
                <h2 className="text-2xl font-black tracking-tight text-white uppercase font-mono">
                  Track<span className="text-[#e5a00d]">Seerr</span>
                  {identity.isGateway && ' Requests'}
                </h2>
                <p className="text-neutral-400 text-xs font-mono">
                  {identity.isGateway ? 'Music Requests' : 'Music Discovery & Request Suite'}
                </p>
              </div>

              {auth.authError && (
                <div className="p-3 bg-red-950/40 border border-red-800 text-xs text-red-300 font-mono rounded-[4px]">
                  {auth.authError}
                </div>
              )}

              <div className="space-y-3 pt-2">
                {!mediaServer.isLoaded ? (
                  <div className="flex items-center justify-center gap-2 text-xs font-mono text-neutral-400">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    <span>Loading...</span>
                  </div>
                ) : !mediaServer.isPlex ? (
                  /* No Plex (none, or a Subsonic server): there is no Plex to sign in with, local accounts only. */
                  <LocalLoginForm login={localLogin} />
                ) : (
                  <>
                    {auth.isAuthenticating ? (
                      <div className="space-y-3">
                        <div className="flex items-center justify-center gap-2 text-xs font-mono text-[#e5a00d]">
                          <Loader2 className="h-4 w-4 animate-spin" />
                          <span>Connecting to Plex...</span>
                        </div>
                        <p className="text-[11px] text-neutral-400 font-mono">
                          {auth.plexAuthPhase === 'popup' &&
                            'Popup window opened. Complete sign-in in the Plex window.'}
                          {auth.plexAuthPhase === 'redirecting' &&
                            'Finishing Plex sign-in. Redirecting to Plex...'}
                          {auth.plexAuthPhase === 'starting' && 'Contacting Plex...'}
                          {auth.plexAuthPhase === 'blocked' &&
                            'Your browser blocked the sign-in popup. Tap the button below to open Plex sign-in.'}
                        </p>
                        {auth.plexAuthPhase === 'blocked' && auth.plexAuthUrl && (
                          <a
                            href={auth.plexAuthUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="tape-deck-btn flex min-h-[36px] w-full items-center justify-center rounded-[3px] px-4 text-xs sm:min-h-[38px]"
                          >
                            Open Plex Sign-In
                          </a>
                        )}
                        <TapeDeckButton
                          size="md"
                          variant="default"
                          onClick={auth.cancelLogin}
                          className="w-full"
                        >
                          Cancel Sign In
                        </TapeDeckButton>
                      </div>
                    ) : (
                      <>
                        <TapeDeckButton
                          size="lg"
                          variant="amber"
                          onClick={auth.loginWithPlex}
                          icon={<LogIn className="h-5 w-5" />}
                          className="w-full"
                        >
                          Sign In with Plex
                        </TapeDeckButton>
                        <div className="flex items-center gap-3 text-[10px] font-mono uppercase text-[var(--text-muted)]">
                          <span className="h-px flex-1 bg-[var(--border-default)]" />
                          or
                          <span className="h-px flex-1 bg-[var(--border-default)]" />
                        </div>
                        {showLocalLogin ? (
                          <LocalLoginForm login={localLogin} />
                        ) : (
                          <TapeDeckButton
                            size="md"
                            className="w-full"
                            onClick={() => setShowLocalLogin(true)}
                          >
                            Sign in with username
                          </TapeDeckButton>
                        )}
                    </>
                  )}
                  </>
                )}
              </div>
            </MachinedCard>
          </div>
          </PageFrame>
        ) : (
          /* Authenticated Dashboard Views: each view is a PageFrame filling this column. */
          <div className="max-w-7xl mx-auto w-full flex flex-col flex-1 min-h-0">
            {activeRoute.tab === 'discover' && <RefreshBinding onRefresh={discovery.refresh} />}
            {activeRoute.tab === 'requests' && (
              <RefreshBinding
                onRefresh={() => Promise.all([requestsHook.refresh(), issuesHook.refresh(), issueCounts.refresh()])}
              />
            )}
            {activeRoute.tab === 'library' && auth.canUseAdminUi && (
              <RefreshBinding onRefresh={libraryHook.reloadCatalog} />
            )}
            {activeRoute.tab === 'playlists' && <RefreshBinding onRefresh={loadPlaylistsAndUsers} />}
            {activeRoute.tab === 'discover' && (
              <DiscoverView
                discovery={discovery}
                route={activeRoute}
                onNavigate={handleNavigate}
                onNavigateUp={navigateUp}
                isAdmin={auth.canUseAdminUi}
                onPlayTrack={audioPlayer.play}
                currentPreviewTrackId={audioPlayer.currentTrack?.id}
                isPreviewPlaying={audioPlayer.isPlaying}
                onRequest={handleRequestItem}
                onRequestDiscography={handleRequestDiscography}
                requestedIds={requestedIds}
                issuesHook={issuesHook}
              />
            )}

            {activeRoute.tab === 'requests' && (
              <RequestsView
                sub={activeRoute.sub}
                onSubChange={(sub, o) => handleNavigate({ tab: 'requests', sub }, o)}
                requestsHook={requestsHook}
                isAdmin={auth.canUseAdminUi}
                canManageRequests={auth.canManageRequests}
                issuesHook={issuesHook}
                issuesUnreadCount={issueCounts.unread}
                currentUserId={auth.user?.id}
                account={accountHook.account}
              />
            )}

            {activeRoute.tab === 'library' && auth.canUseAdminUi && (
              <LibraryView
                route={activeRoute}
                onNavigate={handleNavigate}
                onNavigateUp={navigateUp}
                onRequestItem={handleRequestItem}
                onRequestDiscography={handleRequestDiscography}
                libraryHook={libraryHook}
                issuesHook={issuesHook}
                isAdmin={auth.canUseAdminUi}
                onPlayTrack={audioPlayer.play}
                currentPreviewTrackId={audioPlayer.currentTrack?.id}
                isPreviewPlaying={audioPlayer.isPlaying}
                requestedIds={requestedIds}
              />
            )}

            {activeTab === 'playlists' && (
              <PlaylistsView
                playlists={playlists}
                users={users}
                currentUserId={auth.user?.id}
                onSync={handleSyncPlaylists}
                onToggleTarget={handleToggleTarget}
                onImport={handleImportPlaylist}
                onToggleActive={handleTogglePlaylistActive}
                onSetMonitorMode={handleSetPlaylistMonitorMode}
                canAutoRequest={canAutoRequestPlaylists}
                onSetAutoRequest={handleSetPlaylistAutoRequest}
                onListeningCreated={loadPlaylistsAndUsers}
                onDelete={handleDeletePlaylist}
                isLoading={isPlaylistsLoading}
                isAdmin={auth.canUseAdminUi}
                hasMediaServer={mediaServer.hasMediaServer}
                serverType={mediaServer.type}
                serverLabel={mediaServer.label}
                canTargetUsers={mediaServer.capabilities.users}
                mixesEnabled={mediaServer.capabilities.mixes}
              />
            )}

            {activeRoute.tab === 'activity' && auth.canUseAdminUi && (
              <ActivityView
                sub={activeRoute.sub}
                onSubChange={(sub, o) => handleNavigate({ tab: 'activity', sub }, o)}
                reviewCount={reviewHealth.count}
                onReviewChanged={() => void reviewHealth.refresh()}
                issuesOpenCount={issueCounts.open}
                issueId={activeRoute.issueId}
                onOpenIssue={(id) => handleNavigate({ tab: 'activity', sub: 'issues', issueId: id })}
                onCloseIssue={() => navigateUp({ tab: 'activity', sub: 'issues' })}
              />
            )}

            {activeRoute.tab === 'wanted' && auth.canUseAdminUi && (
              <WantedView
                sub={activeRoute.sub}
                onSubChange={(sub, o) => handleNavigate({ tab: 'wanted', sub }, o)}
              />
            )}

            {activeRoute.tab === 'settings' && (
              <SettingsView
                route={activeRoute}
                onNavigate={handleNavigate}
                isAdmin={auth.canUseAdminUi}
                showGatewayNote={auth.isAdmin && auth.tier === 'gateway'}
                isCore={identity.isCore}
                accountHook={accountHook}
                currentUserId={auth.user?.id}
                mfaEnrollmentRequired={mfaEnrollmentRequired}
                hasMediaServer={mediaServer.isPlex}
              />
            )}
            {identity.isGateway && (
              <p className="shrink-0 pt-2 text-center text-[10px] font-mono text-[var(--text-muted)]">
                Settings are managed in TrackSeerr Core
              </p>
            )}
          </div>
        )}
      </main>

      {/* Persistent Audio Player Bar */}
      <AudioPlayerBar
        currentTrack={audioPlayer.currentTrack}
        isPlaying={audioPlayer.isPlaying}
        progress={audioPlayer.progress}
        duration={audioPlayer.duration}
        currentTime={audioPlayer.currentTime}
        onToggle={audioPlayer.toggle}
        onStop={audioPlayer.stop}
        onSeek={audioPlayer.seek}
      />

      {/* Plex Sign-In Modal */}
      <ObsidianModal
        isOpen={isAuthModalOpen && mediaServer.isPlex}
        onClose={() => {
          setIsAuthModalOpen(false);
          auth.cancelLogin();
        }}
        title="Sign In with Plex"
        subtitle="Authorize your account via Plex OAuth"
      >
        <div className="space-y-5 text-center py-4">
          <div className="flex justify-center">
            <div className="h-16 w-16 rounded-[4px] bg-[#141414] border border-[#262626] flex items-center justify-center shadow-lg">
              <img
                src="/trackseerr-logo.svg"
                alt="TrackSeerr"
                className="h-14 w-14 object-contain mx-auto"
                onError={(e) => {
                  (e.currentTarget as HTMLImageElement).src = '/static/trackseerr-logo.svg';
                }}
              />
            </div>
          </div>

          <div className="space-y-1">
            <h4 className="font-bold text-sm sm:text-base text-white">Authorize with Plex</h4>
            <p className="text-xs text-neutral-400 max-w-sm mx-auto font-mono">
              Sign in with your plex.tv credentials to manage playlists, browse recommendations,
              and submit music requests.
            </p>
          </div>

          {auth.authError && (
            <div className="p-3 bg-red-950/40 border border-red-800 text-xs text-red-300 font-mono rounded-[4px]">
              {auth.authError}
            </div>
          )}

          <div className="flex flex-col items-center gap-3 pt-2">
            <TapeDeckButton
              size="lg"
              variant="amber"
              onClick={async () => {
                await auth.loginWithPlex();
              }}
              disabled={auth.isAuthenticating}
              icon={
                auth.isAuthenticating ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <LogIn className="h-4 w-4" />
                )
              }
              className="w-full max-w-xs"
            >
              {auth.isAuthenticating ? 'Waiting for Plex...' : 'Authorize with Plex'}
            </TapeDeckButton>

            {auth.isAuthenticating && (
              <p className="text-[11px] text-neutral-500 font-mono animate-pulse">
                Popup open. Complete the sign-in prompt in the Plex window.
              </p>
            )}
          </div>
        </div>
      </ObsidianModal>
    </div>
    </RefreshContext.Provider>
  );
};

export const App: React.FC = () => {
  const startup = useStartupStatus();
  const inviteToken = parseInviteToken(window.location.pathname);
  // Hold the whole app (hooks included) back until the server is up, so nothing fires 503-ing requests.
  if (startup.phase === 'checking') return <div className="h-full bg-[#0a0a0a]" />;
  if (startup.phase === 'starting') return <StartupScreen step={startup.step} />;
  // The invite page must work without a session, so it bypasses auth (and its hooks) entirely.
  return inviteToken ? <InvitePage token={inviteToken} /> : <MainApp />;
};

export default App;

