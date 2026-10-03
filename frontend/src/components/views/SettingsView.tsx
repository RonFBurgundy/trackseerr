import React, { useState, useEffect } from 'react';
import {
  Sliders,
  Folder,
  Download,
  Search,
  Radio,
  Layers,
  Activity,
  Check,
  Save,
  Loader2,
  Trash2,
  Plus,
  Play,
  Square,
  RotateCw,
  UserRound,
  Users,
} from 'lucide-react';
import type {
  GeneralSettings,
  QualityProfile,
  DownloadClientItem,
  IndexerItem,
  SystemStatusInfo,
  MediaManagementSettings,
  LidarrSettings,
  ScheduledTaskItem,
} from '@/types/models';
import {
  TapeTransportBay,
  TapeDeckButton,
  MachinedCard,
  TactileSwitch,
} from '@/components/ui';
import {
  getGeneralSettings,
  updateGeneralSettings,
  getQualityProfiles,
  saveQualityProfile,
  deleteQualityProfile,
  getClientSettings,
  saveClientSettings,
  deleteClientSettings,
  testClientConnection,
  getIndexerSettings,
  saveIndexer,
  deleteIndexer,
  testIndexer,
  getSystemStatus,
  getMediaManagementSettings,
  updateMediaManagementSettings,
  getLidarrSettings,
  updateLidarrSettings,
  testLidarrConnection,
} from '@/services/settingsService';
import { ScrobblingSettings } from '@/components/scrobbling';
import { NamingFormatsEditor } from '@/components/naming/NamingFormatsEditor';
import { AccountPanel } from '@/components/account';
import { UsersPanel } from '@/components/admin';
import { RequestPortalCard, RoleChangeBanner } from '@/components/deployment';
import type { UseAccountReturn } from '@/hooks/useAccount';
import { useAdminUsers } from '@/hooks/useAdminUsers';
import {
  getScheduledTasks,
  triggerScheduledTask,
  cancelScheduledTask,
} from '@/services/systemService';

export type SettingsTab =
  | 'general'
  | 'media'
  | 'clients'
  | 'indexers'
  | 'lidarr'
  | 'profiles'
  | 'tasks'
  | 'status'
  | 'scrobbling'
  | 'account'
  | 'users';

export interface SettingsViewProps {
  isAdmin?: boolean;
  showGatewayNote?: boolean;
  /** Core tier only: shows the Request portal card under System. */
  isCore?: boolean;
  accountHook: UseAccountReturn;
  currentUserId?: string | number;
  /** Local sign-in succeeded but MFA enrollment is mandatory: only the Account tab is usable. */
  mfaEnrollmentRequired?: boolean;
}

export const SettingsView: React.FC<SettingsViewProps> = ({
  isAdmin = false,
  showGatewayNote = false,
  isCore = false,
  accountHook,
  currentUserId,
  mfaEnrollmentRequired = false,
}) => {
  const [activeTab, setActiveTab] = useState<SettingsTab>(() =>
    mfaEnrollmentRequired
      ? 'account'
      : !isAdmin || new URLSearchParams(window.location.search).has('connected') ||
    new URLSearchParams(window.location.search).has('scrobble_error')
      ? 'scrobbling'
      : 'general'
  );
  const adminUsersHook = useAdminUsers(isAdmin && activeTab === 'users' && !mfaEnrollmentRequired);
  const [generalSettings, setGeneralSettings] = useState<GeneralSettings | null>(null);
  const [mediaSettings, setMediaSettings] = useState<MediaManagementSettings | null>(null);
  const [lidarrSettings, setLidarrSettings] = useState<LidarrSettings | null>(null);
  const [qualityProfiles, setQualityProfiles] = useState<QualityProfile[]>([]);
  const [clients, setClients] = useState<DownloadClientItem[]>([]);
  const [indexers, setIndexers] = useState<IndexerItem[]>([]);
  const [systemStatus, setSystemStatus] = useState<SystemStatusInfo | null>(null);
  const [tasks, setTasks] = useState<ScheduledTaskItem[]>([]);
  const [runningTaskIds, setRunningTaskIds] = useState<Set<string>>(new Set());
  const [cancellingTaskIds, setCancellingTaskIds] = useState<Set<string>>(new Set());
  const [isLoadingTasks, setIsLoadingTasks] = useState<boolean>(false);

  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [isTestingLidarr, setIsTestingLidarr] = useState<boolean>(false);
  const [toastMessage, setToastMessage] = useState<string | null>(null);

  // New item form states
  const [newClientName, setNewClientName] = useState<string>('');
  const [newClientType, setNewClientType] = useState<DownloadClientItem['client_type']>('slskd');
  const [newClientHost, setNewClientHost] = useState<string>('localhost');
  const [newClientPort, setNewClientPort] = useState<number>(5030);

  const [newIndexerName, setNewIndexerName] = useState<string>('');
  const [newIndexerUrl, setNewIndexerUrl] = useState<string>('');
  const [newIndexerKey, setNewIndexerKey] = useState<string>('');

  const [newProfileName, setNewProfileName] = useState<string>('');
  const [newProfileCutoff, setNewProfileCutoff] = useState<number>(1);

  const showToast = (msg: string) => {
    setToastMessage(msg);
    setTimeout(() => setToastMessage(null), 3000);
  };

  const loadTasks = async () => {
    setIsLoadingTasks(true);
    try {
      const data = await getScheduledTasks();
      setTasks(data);
    } catch {
      showToast('Failed to load scheduled tasks');
    } finally {
      setIsLoadingTasks(false);
    }
  };

  const handleRunTask = async (taskId: string) => {
    setRunningTaskIds((prev) => new Set([...prev, taskId]));
    try {
      const res = await triggerScheduledTask(taskId);
      showToast(res.message || `Task '${taskId}' dispatched`);
      await loadTasks();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to trigger task';
      showToast(msg);
    } finally {
      setRunningTaskIds((prev) => {
        const next = new Set(prev);
        next.delete(taskId);
        return next;
      });
    }
  };

  const handleCancelTask = async (taskId: string) => {
    setCancellingTaskIds((prev) => new Set([...prev, taskId]));
    try {
      const res = await cancelScheduledTask(taskId);
      showToast(res.message || `Task '${taskId}' cancelled`);
      await loadTasks();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to cancel task';
      showToast(msg);
    } finally {
      setCancellingTaskIds((prev) => {
        const next = new Set(prev);
        next.delete(taskId);
        return next;
      });
    }
  };

  const loadData = async () => {
    setIsLoading(true);
    try {
      const [gen, med, lid, prof, cli, idx, sys, tsk] = await Promise.all([
        getGeneralSettings().catch(() => null),
        getMediaManagementSettings().catch(() => null),
        getLidarrSettings().catch(() => null),
        getQualityProfiles().catch(() => []),
        getClientSettings().catch(() => []),
        getIndexerSettings().catch(() => []),
        getSystemStatus().catch(() => null),
        getScheduledTasks().catch(() => []),
      ]);
      if (gen) setGeneralSettings(gen);
      if (med) setMediaSettings(med);
      if (lid) setLidarrSettings(lid);
      setQualityProfiles(prof);
      setClients(cli);
      setIndexers(idx);
      if (sys) setSystemStatus(sys);
      setTasks(tsk);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (isAdmin) void loadData();
    else setIsLoading(false);
  }, [isAdmin]);

  useEffect(() => {
    if (mfaEnrollmentRequired) setActiveTab('account');
  }, [mfaEnrollmentRequired]);

  useEffect(() => {
    if (activeTab === 'tasks') {
      loadTasks();
    }
  }, [activeTab]);

  const handleSaveGeneral = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!generalSettings) return;
    setIsSaving(true);
    try {
      await updateGeneralSettings(generalSettings);
      showToast('General settings saved successfully');
    } catch {
      showToast('Failed to save settings');
    } finally {
      setIsSaving(false);
    }
  };

  const handleSaveMedia = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!mediaSettings) return;
    setIsSaving(true);
    try {
      const updated = await updateMediaManagementSettings(mediaSettings);
      setMediaSettings(updated);
      showToast('Media management settings saved');
    } catch {
      showToast('Failed to save media management settings');
    } finally {
      setIsSaving(false);
    }
  };

  const handleSaveLidarr = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!lidarrSettings) return;
    setIsSaving(true);
    try {
      const updated = await updateLidarrSettings(lidarrSettings);
      setLidarrSettings(updated);
      showToast('Lidarr settings saved');
    } catch {
      showToast('Failed to save Lidarr settings');
    } finally {
      setIsSaving(false);
    }
  };

  const handleTestLidarr = async () => {
    if (!lidarrSettings?.url) {
      showToast('Lidarr URL is required to test');
      return;
    }
    setIsTestingLidarr(true);
    try {
      const res = await testLidarrConnection({
        url: lidarrSettings.url,
        api_key: lidarrSettings.api_key || '',
      });
      if (res.online) {
        showToast(`Lidarr online! Version: ${res.version || 'OK'}`);
      } else {
        showToast(`Connection failed: ${res.error || 'Offline'}`);
      }
    } catch {
      showToast('Error testing Lidarr connection');
    } finally {
      setIsTestingLidarr(false);
    }
  };

  const handleToggleUpgradeAllowed = async (profile: QualityProfile) => {
    const newAllowed = !profile.upgrade_allowed;
    setQualityProfiles((prev) =>
      prev.map((pr) => (pr.id === profile.id ? { ...pr, upgrade_allowed: newAllowed } : pr))
    );
    try {
      await saveQualityProfile({ ...profile, upgrade_allowed: newAllowed });
      showToast(`Updated ${profile.name}`);
    } catch {
      showToast('Failed to update quality profile');
      loadData();
    }
  };

  const handleAddClient = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newClientName.trim()) return;
    setIsSaving(true);
    try {
      await saveClientSettings({
        name: newClientName.trim(),
        client_type: newClientType,
        host: newClientHost,
        port: newClientPort,
        use_ssl: false,
        is_enabled: true,
        priority: 1,
      });
      setNewClientName('');
      await loadData();
      showToast('Download client added');
    } catch {
      showToast('Failed to add client');
    } finally {
      setIsSaving(false);
    }
  };

  const handleDeleteClient = async (id: number) => {
    if (!confirm('Delete this download client?')) return;
    try {
      await deleteClientSettings(id);
      await loadData();
      showToast('Client deleted');
    } catch {
      showToast('Failed to delete client');
    }
  };

  const handleTestClient = async (client: DownloadClientItem) => {
    try {
      const res = await testClientConnection(client);
      showToast(res.success ? 'Connection verified!' : `Connection failed: ${res.message}`);
    } catch {
      showToast('Connection test error');
    }
  };

  const handleAddIndexer = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newIndexerName.trim() || !newIndexerUrl.trim()) return;
    setIsSaving(true);
    try {
      await saveIndexer({
        name: newIndexerName.trim(),
        url: newIndexerUrl.trim(),
        api_key: newIndexerKey.trim(),
        indexer_type: 'torznab',
        is_enabled: true,
        priority: 1,
      });
      setNewIndexerName('');
      setNewIndexerUrl('');
      setNewIndexerKey('');
      await loadData();
      showToast('Indexer registered');
    } catch {
      showToast('Failed to save indexer');
    } finally {
      setIsSaving(false);
    }
  };

  const handleDeleteIndexer = async (id: number) => {
    if (!confirm('Delete this indexer?')) return;
    try {
      await deleteIndexer(id);
      await loadData();
      showToast('Indexer removed');
    } catch {
      showToast('Failed to delete indexer');
    }
  };

  const handleTestIndexer = async (indexer: IndexerItem) => {
    try {
      const res = await testIndexer(indexer);
      showToast(res.success ? 'Indexer responded OK' : `Indexer error: ${res.message}`);
    } catch {
      showToast('Test indexer failed');
    }
  };

  const handleAddProfile = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProfileName.trim()) return;
    setIsSaving(true);
    try {
      await saveQualityProfile({
        name: newProfileName.trim(),
        cutoff: newProfileCutoff,
        upgrade_allowed: true,
      });
      setNewProfileName('');
      await loadData();
      showToast('Quality profile created');
    } catch {
      showToast('Failed to save profile');
    } finally {
      setIsSaving(false);
    }
  };

  const handleDeleteProfile = async (id: number) => {
    if (!confirm('Delete this quality profile?')) return;
    try {
      await deleteQualityProfile(id);
      await loadData();
      showToast('Profile deleted');
    } catch {
      showToast('Failed to delete profile');
    }
  };

  const subTabs: Array<{ id: SettingsTab; label: string; icon: React.ReactNode }> = [
    { id: 'general', label: 'General', icon: <Sliders className="h-3.5 w-3.5" /> },
    { id: 'media', label: 'Media', icon: <Folder className="h-3.5 w-3.5" /> },
    { id: 'clients', label: 'Clients', icon: <Download className="h-3.5 w-3.5" /> },
    { id: 'indexers', label: 'Indexers', icon: <Search className="h-3.5 w-3.5" /> },
    { id: 'lidarr', label: 'Lidarr', icon: <Radio className="h-3.5 w-3.5" /> },
    { id: 'profiles', label: 'Profiles', icon: <Layers className="h-3.5 w-3.5" /> },
    { id: 'tasks', label: 'Tasks', icon: <Activity className="h-3.5 w-3.5" /> },
    { id: 'status', label: 'Status', icon: <Activity className="h-3.5 w-3.5" /> },
  ];
  const scrobblingTab = { id: 'scrobbling' as const, label: 'Scrobbling', icon: <Radio className="h-3.5 w-3.5" /> };
  const accountTab = { id: 'account' as const, label: 'Account', icon: <UserRound className="h-3.5 w-3.5" /> };
  const usersTab = { id: 'users' as const, label: 'Users', icon: <Users className="h-3.5 w-3.5" /> };
  const visibleTabs = mfaEnrollmentRequired
    ? [accountTab]
    : isAdmin
    ? [...subTabs, scrobblingTab, accountTab, usersTab]
    : [scrobblingTab, accountTab];
  const isSelfServiceTab = activeTab === 'scrobbling' || activeTab === 'account' || activeTab === 'users';

  return (
    <div className="space-y-6">
      {/* Toast Notification */}
      {toastMessage && (
        <div className="fixed top-20 right-4 z-50 bg-[#161616] border border-[#e5a00d] px-4 py-2.5 rounded-[4px] text-xs font-mono text-[#e5a00d] shadow-lg flex items-center gap-2">
          <Check className="h-4 w-4" />
          <span>{toastMessage}</span>
        </div>
      )}

      <RoleChangeBanner enabled={isAdmin && !mfaEnrollmentRequired} />

      {showGatewayNote && (
        <div className="bg-[#121212] border border-[#2a2a2a] rounded-[4px] px-4 py-3 text-xs font-mono text-neutral-400">
          Admin settings are available on the TrackSeerr Core admin interface.
        </div>
      )}

      {/* Subtab Navigation Bar */}
      <TapeTransportBay className="flex items-center gap-1.5 overflow-x-auto">
        {visibleTabs.map((st) => (
          <TapeDeckButton
            key={st.id}
            size="sm"
            active={activeTab === st.id}
            onClick={() => setActiveTab(st.id)}
            icon={st.icon}
          >
            {st.label}
          </TapeDeckButton>
        ))}
      </TapeTransportBay>

      {activeTab === 'scrobbling' && !mfaEnrollmentRequired && <ScrobblingSettings isAdmin={isAdmin} />}

      {activeTab === 'account' && (
        <AccountPanel
          accountHook={accountHook}
          isAdmin={isAdmin}
          enrollmentBlocking={mfaEnrollmentRequired}
        />
      )}

      {activeTab === 'users' && isAdmin && !mfaEnrollmentRequired && (
        <UsersPanel adminHook={adminUsersHook} currentUserId={currentUserId} />
      )}

      {/* Loading state */}
      {isLoading && !isSelfServiceTab && (
        <div className="flex flex-col items-center justify-center py-16 gap-3">
          <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
          <span className="text-xs uppercase tracking-widest text-neutral-400 font-mono">
            Reading System Configuration...
          </span>
        </div>
      )}

      {/* General Settings Subtab */}
      {!isLoading && activeTab === 'general' && (
        <MachinedCard className="p-6 max-w-2xl">
          <form onSubmit={handleSaveGeneral} className="space-y-5">
            <div>
              <label className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5">
                Server Name
              </label>
              <input
                type="text"
                value={generalSettings?.server_name || ''}
                onChange={(e) =>
                  setGeneralSettings((prev) =>
                    prev ? { ...prev, server_name: e.target.value } : null
                  )
                }
                className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]"
              />
            </div>

            <div>
              <label className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5">
                Base URL
              </label>
              <input
                type="text"
                value={generalSettings?.base_url || ''}
                onChange={(e) =>
                  setGeneralSettings((prev) =>
                    prev ? { ...prev, base_url: e.target.value } : null
                  )
                }
                className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]"
              />
            </div>

            <div className="flex justify-end pt-3">
              <TapeDeckButton
                type="submit"
                variant="amber"
                size="md"
                disabled={isSaving}
                icon={
                  isSaving ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Save className="h-4 w-4" />
                  )
                }
              >
                Save General Settings
              </TapeDeckButton>
            </div>
          </form>
        </MachinedCard>
      )}

      {/* Media Management Subtab */}
      {!isLoading && activeTab === 'media' && (
        <MachinedCard className="p-6 max-w-2xl space-y-5">
          <div className="border-b border-[#222222] pb-3">
            <h4 className="text-sm font-bold uppercase font-mono text-white">Media Management &amp; Token Templates</h4>
            <p className="text-xs text-neutral-400 font-mono mt-0.5">Configure library paths, naming templates, and audio tagging</p>
          </div>

          <form onSubmit={handleSaveMedia} className="space-y-4">
            <div>
              <label className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5">
                Root Music Folder
              </label>
              <input
                type="text"
                value={mediaSettings?.root_folder_path || ''}
                onChange={(e) =>
                  setMediaSettings((prev) =>
                    prev ? { ...prev, root_folder_path: e.target.value } : null
                  )
                }
                placeholder="/data/media/music"
                className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d] font-mono"
              />
            </div>

            <div>
              <label className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5">
                Staging / Downloads Folder
              </label>
              <input
                type="text"
                value={mediaSettings?.staging_folder_path || ''}
                onChange={(e) =>
                  setMediaSettings((prev) =>
                    prev ? { ...prev, staging_folder_path: e.target.value } : null
                  )
                }
                placeholder="/data/downloads"
                className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d] font-mono"
              />
            </div>

            <NamingFormatsEditor
              value={{
                artist_folder_format: mediaSettings?.artist_folder_format || '',
                standard_track_format: mediaSettings?.standard_track_format || '',
                multi_disc_track_format: mediaSettings?.multi_disc_track_format || '',
                compilation_track_format: mediaSettings?.compilation_track_format || '',
              }}
              context={{
                root_folder_path: mediaSettings?.root_folder_path,
                colon_replacement_format: mediaSettings?.colon_replacement_format,
                clean_artist_names: mediaSettings?.clean_artist_names,
              }}
              onChange={(patch) => setMediaSettings((prev) => (prev ? { ...prev, ...patch } : null))}
            />

            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 pt-2 border-t border-[#1f1f1f]">
              <div>
                <label className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5">
                  Import Mode
                </label>
                <select
                  value={mediaSettings?.import_mode || 'move'}
                  onChange={(e) =>
                    setMediaSettings((prev) =>
                      prev ? { ...prev, import_mode: e.target.value as 'move' | 'hardlink' | 'copy' } : null
                    )
                  }
                  className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]"
                >
                  <option value="move">Move</option>
                  <option value="hardlink">Hardlink</option>
                  <option value="copy">Copy</option>
                </select>
              </div>

              <div className="flex flex-col justify-end">
                <div className="flex items-center justify-between pb-2">
                  <span className="text-xs font-mono text-neutral-300">Normalize Audio Tags</span>
                  <TactileSwitch
                    checked={mediaSettings?.write_audio_tags ?? true}
                    onChange={(val) =>
                      setMediaSettings((prev) =>
                        prev ? { ...prev, write_audio_tags: val } : null
                      )
                    }
                    label="Write Tags"
                  />
                </div>
              </div>

              <div className="flex flex-col justify-end">
                <div className="flex items-center justify-between pb-2">
                  <span className="text-xs font-mono text-neutral-300">Embed Artwork</span>
                  <TactileSwitch
                    checked={mediaSettings?.embed_artwork ?? true}
                    onChange={(val) =>
                      setMediaSettings((prev) =>
                        prev ? { ...prev, embed_artwork: val } : null
                      )
                    }
                    label="Embed Artwork"
                  />
                </div>
              </div>
            </div>

            <div className="flex justify-end pt-3">
              <TapeDeckButton
                type="submit"
                variant="amber"
                size="md"
                disabled={isSaving}
                icon={
                  isSaving ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Save className="h-4 w-4" />
                  )
                }
              >
                Save Media Settings
              </TapeDeckButton>
            </div>
          </form>
        </MachinedCard>
      )}

      {/* Download Clients Subtab */}
      {!isLoading && activeTab === 'clients' && (
        <div className="space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {clients.map((c) => (
              <MachinedCard key={c.id} className="p-4 flex items-center justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-bold text-sm text-white">{c.name}</span>
                    <span className="px-1.5 py-0.5 rounded-[2px] bg-[#1a1a1a] text-[10px] font-mono uppercase text-[#e5a00d]">
                      {c.client_type}
                    </span>
                  </div>
                  <p className="text-xs text-neutral-400 font-mono mt-1">
                    {c.host}:{c.port}
                  </p>
                </div>

                <div className="flex items-center gap-2">
                  <TapeDeckButton
                    size="sm"
                    onClick={() => handleTestClient(c)}
                  >
                    Test
                  </TapeDeckButton>
                  <TapeDeckButton
                    size="sm"
                    variant="danger"
                    onClick={() => handleDeleteClient(c.id)}
                    icon={<Trash2 className="h-3 w-3" />}
                    aria-label="Delete client"
                  />
                </div>
              </MachinedCard>
            ))}
          </div>

          <MachinedCard className="p-5 max-w-xl">
            <h4 className="text-xs font-bold uppercase font-mono text-white mb-4">
              Add Download Client
            </h4>
            <form onSubmit={handleAddClient} className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[11px] font-mono text-neutral-300 mb-1">Name</label>
                  <input
                    type="text"
                    required
                    value={newClientName}
                    onChange={(e) => setNewClientName(e.target.value)}
                    placeholder="e.g. Local Soulseek"
                    className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-2.5 py-1.5 text-xs text-white"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-mono text-neutral-300 mb-1">Type</label>
                  <select
                    value={newClientType}
                    onChange={(e) =>
                      setNewClientType(e.target.value as DownloadClientItem['client_type'])
                    }
                    className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-2.5 py-1.5 text-xs text-white"
                  >
                    <option value="slskd">slskd</option>
                    <option value="sabnzbd">SABnzbd</option>
                    <option value="qbittorrent">qBittorrent</option>
                  </select>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[11px] font-mono text-neutral-300 mb-1">Host</label>
                  <input
                    type="text"
                    required
                    value={newClientHost}
                    onChange={(e) => setNewClientHost(e.target.value)}
                    className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-2.5 py-1.5 text-xs text-white"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-mono text-neutral-300 mb-1">Port</label>
                  <input
                    type="number"
                    required
                    value={newClientPort}
                    onChange={(e) => setNewClientPort(Number(e.target.value))}
                    className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-2.5 py-1.5 text-xs text-white"
                  />
                </div>
              </div>

              <div className="flex justify-end pt-2">
                <TapeDeckButton
                  type="submit"
                  size="sm"
                  variant="amber"
                  disabled={isSaving}
                  icon={<Plus className="h-3.5 w-3.5" />}
                >
                  Add Client
                </TapeDeckButton>
              </div>
            </form>
          </MachinedCard>
        </div>
      )}

      {/* Indexers Subtab */}
      {!isLoading && activeTab === 'indexers' && (
        <div className="space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {indexers.map((idx) => (
              <MachinedCard key={idx.id} className="p-4 flex items-center justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-bold text-sm text-white">{idx.name}</span>
                    <span className="px-1.5 py-0.5 rounded-[2px] bg-[#1a1a1a] text-[10px] font-mono uppercase text-[#e5a00d]">
                      {idx.indexer_type}
                    </span>
                  </div>
                  <p className="text-xs text-neutral-400 font-mono mt-1 truncate max-w-xs">
                    {idx.url}
                  </p>
                </div>

                <div className="flex items-center gap-2">
                  <TapeDeckButton
                    size="sm"
                    onClick={() => handleTestIndexer(idx)}
                  >
                    Test
                  </TapeDeckButton>
                  <TapeDeckButton
                    size="sm"
                    variant="danger"
                    onClick={() => handleDeleteIndexer(idx.id)}
                    icon={<Trash2 className="h-3 w-3" />}
                    aria-label="Delete indexer"
                  />
                </div>
              </MachinedCard>
            ))}
          </div>

          <MachinedCard className="p-5 max-w-xl">
            <h4 className="text-xs font-bold uppercase font-mono text-white mb-4">
              Add New Indexer (Torznab / Newznab)
            </h4>
            <form onSubmit={handleAddIndexer} className="space-y-4">
              <div>
                <label className="block text-[11px] font-mono text-neutral-300 mb-1">Name</label>
                <input
                  type="text"
                  required
                  value={newIndexerName}
                  onChange={(e) => setNewIndexerName(e.target.value)}
                  placeholder="e.g. Redacted Torznab"
                  className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-2.5 py-1.5 text-xs text-white"
                />
              </div>

              <div>
                <label className="block text-[11px] font-mono text-neutral-300 mb-1">URL</label>
                <input
                  type="url"
                  required
                  value={newIndexerUrl}
                  onChange={(e) => setNewIndexerUrl(e.target.value)}
                  placeholder="http://prowlarr:9696/1/api"
                  className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-2.5 py-1.5 text-xs text-white"
                />
              </div>

              <div>
                <label className="block text-[11px] font-mono text-neutral-300 mb-1">API Key</label>
                <input
                  type="password"
                  value={newIndexerKey}
                  onChange={(e) => setNewIndexerKey(e.target.value)}
                  className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-2.5 py-1.5 text-xs text-white"
                />
              </div>

              <div className="flex justify-end pt-2">
                <TapeDeckButton
                  type="submit"
                  size="sm"
                  variant="amber"
                  disabled={isSaving}
                  icon={<Plus className="h-3.5 w-3.5" />}
                >
                  Save Indexer
                </TapeDeckButton>
              </div>
            </form>
          </MachinedCard>
        </div>
      )}

      {/* Lidarr Subtab */}
      {!isLoading && activeTab === 'lidarr' && (
        <MachinedCard className="p-6 max-w-2xl space-y-5">
          <div className="flex items-center justify-between border-b border-[#222222] pb-3">
            <div>
              <h4 className="text-sm font-bold uppercase font-mono text-white">Lidarr Integration</h4>
              <p className="text-xs text-neutral-400 font-mono mt-0.5">Automated music acquisition &amp; trickle sync</p>
            </div>
            <TapeDeckButton
              type="button"
              size="sm"
              disabled={isTestingLidarr || !lidarrSettings?.url}
              onClick={handleTestLidarr}
              icon={isTestingLidarr ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
            >
              Test Connection
            </TapeDeckButton>
          </div>

          <form onSubmit={handleSaveLidarr} className="space-y-4">
            <div>
              <label className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5">
                Lidarr Host URL
              </label>
              <input
                type="text"
                value={lidarrSettings?.url || ''}
                onChange={(e) =>
                  setLidarrSettings((prev) =>
                    prev
                      ? { ...prev, url: e.target.value }
                      : {
                          url: e.target.value,
                          auto_search: true,
                          auto_trickle: false,
                          trickle_rate_seconds: 3.0,
                          trickle_batch_size: 25,
                        }
                  )
                }
                placeholder="http://localhost:8686"
                className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]"
              />
            </div>

            <div>
              <label className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5">
                Lidarr API Key
              </label>
              <input
                type="password"
                value={lidarrSettings?.api_key || ''}
                onChange={(e) =>
                  setLidarrSettings((prev) =>
                    prev
                      ? { ...prev, api_key: e.target.value }
                      : {
                          url: '',
                          api_key: e.target.value,
                          auto_search: true,
                          auto_trickle: false,
                          trickle_rate_seconds: 3.0,
                          trickle_batch_size: 25,
                        }
                  )
                }
                placeholder="Leave blank or masked to keep current key"
                className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]"
              />
            </div>

            <div className="pt-2 border-t border-[#1f1f1f] space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <span className="text-xs font-mono font-medium text-white block">Automatic Search</span>
                  <span className="text-[11px] text-neutral-400 font-mono">Trigger search in Lidarr when releases are requested</span>
                </div>
                <TactileSwitch
                  checked={lidarrSettings?.auto_search ?? true}
                  onChange={(val) =>
                    setLidarrSettings((prev) =>
                      prev ? { ...prev, auto_search: val } : null
                    )
                  }
                  label="Auto Search"
                />
              </div>

              <div className="flex items-center justify-between">
                <div>
                  <span className="text-xs font-mono font-medium text-white block">Auto Trickle Sync</span>
                  <span className="text-[11px] text-neutral-400 font-mono">Pace artist ingest calls to prevent Lidarr rate-limiting</span>
                </div>
                <TactileSwitch
                  checked={lidarrSettings?.auto_trickle ?? false}
                  onChange={(val) =>
                    setLidarrSettings((prev) =>
                      prev ? { ...prev, auto_trickle: val } : null
                    )
                  }
                  label="Auto Trickle"
                />
              </div>

              <div className="grid grid-cols-2 gap-3 pt-2">
                <div>
                  <label className="block text-[11px] font-mono text-neutral-300 mb-1">
                    Trickle Rate (Seconds)
                  </label>
                  <input
                    type="number"
                    step="0.5"
                    min="0.5"
                    value={lidarrSettings?.trickle_rate_seconds ?? 3.0}
                    onChange={(e) =>
                      setLidarrSettings((prev) =>
                        prev ? { ...prev, trickle_rate_seconds: parseFloat(e.target.value) || 3.0 } : null
                      )
                    }
                    className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-2.5 py-1.5 text-xs text-white"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-mono text-neutral-300 mb-1">
                    Trickle Batch Size
                  </label>
                  <input
                    type="number"
                    min="1"
                    value={lidarrSettings?.trickle_batch_size ?? 25}
                    onChange={(e) =>
                      setLidarrSettings((prev) =>
                        prev ? { ...prev, trickle_batch_size: parseInt(e.target.value, 10) || 25 } : null
                      )
                    }
                    className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-2.5 py-1.5 text-xs text-white"
                  />
                </div>
              </div>
            </div>

            <div className="flex justify-end pt-3">
              <TapeDeckButton
                type="submit"
                variant="amber"
                size="md"
                disabled={isSaving}
                icon={
                  isSaving ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Save className="h-4 w-4" />
                  )
                }
              >
                Save Lidarr Settings
              </TapeDeckButton>
            </div>
          </form>
        </MachinedCard>
      )}

      {/* Quality Profiles Subtab */}
      {!isLoading && activeTab === 'profiles' && (
        <div className="space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {qualityProfiles.map((p) => (
              <MachinedCard key={p.id} className="p-4 flex items-center justify-between gap-3">
                <div>
                  <span className="font-bold text-sm text-white">{p.name}</span>
                  <p className="text-xs text-neutral-400 font-mono mt-1">
                    Cutoff Tier: {p.cutoff}
                  </p>
                </div>

                <div className="flex items-center gap-2">
                  <TactileSwitch
                    checked={Boolean(p.upgrade_allowed)}
                    onChange={() => handleToggleUpgradeAllowed(p)}
                    label="Upgrade"
                  />
                  <TapeDeckButton
                    size="sm"
                    variant="danger"
                    onClick={() => handleDeleteProfile(p.id)}
                    icon={<Trash2 className="h-3 w-3" />}
                    aria-label="Delete profile"
                  />
                </div>
              </MachinedCard>
            ))}
          </div>

          <MachinedCard className="p-5 max-w-xl">
            <h4 className="text-xs font-bold uppercase font-mono text-white mb-4">
              Add Quality Profile
            </h4>
            <form onSubmit={handleAddProfile} className="space-y-4">
              <div>
                <label className="block text-[11px] font-mono text-neutral-300 mb-1">
                  Profile Name
                </label>
                <input
                  type="text"
                  required
                  value={newProfileName}
                  onChange={(e) => setNewProfileName(e.target.value)}
                  placeholder="e.g. FLAC Lossless Only"
                  className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-2.5 py-1.5 text-xs text-white"
                />
              </div>

              <div>
                <label className="block text-[11px] font-mono text-neutral-300 mb-1">
                  Cutoff Score / Level
                </label>
                <input
                  type="number"
                  required
                  value={newProfileCutoff}
                  onChange={(e) => setNewProfileCutoff(Number(e.target.value))}
                  className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-2.5 py-1.5 text-xs text-white"
                />
              </div>

              <div className="flex justify-end pt-2">
                <TapeDeckButton
                  type="submit"
                  size="sm"
                  variant="amber"
                  disabled={isSaving}
                  icon={<Plus className="h-3.5 w-3.5" />}
                >
                  Create Profile
                </TapeDeckButton>
              </div>
            </form>
          </MachinedCard>
        </div>
      )}

      {/* Scheduled Tasks Subtab */}
      {!isLoading && activeTab === 'tasks' && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h4 className="text-sm font-bold uppercase font-mono text-white">
                Scheduled Tasks &amp; Background Workers
              </h4>
              <p className="text-xs text-neutral-400 font-mono mt-0.5">
                Monitor recurring automation timers, intervals, and trigger on-demand sweeps
              </p>
            </div>
            <TapeDeckButton
              size="sm"
              onClick={loadTasks}
              disabled={isLoadingTasks}
              icon={
                isLoadingTasks ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <RotateCw className="h-3.5 w-3.5" />
                )
              }
            >
              Refresh
            </TapeDeckButton>
          </div>

          <MachinedCard className="overflow-hidden p-0 border-[#222222]">
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="border-b border-[#222222] bg-[#121212] text-[11px] font-mono uppercase tracking-wider text-neutral-400">
                    <th className="py-3 px-4">Task</th>
                    <th className="py-3 px-4">Interval</th>
                    <th className="py-3 px-4">Status</th>
                    <th className="py-3 px-4">Last Run</th>
                    <th className="py-3 px-4 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#1c1c1c] text-xs font-mono">
                  {tasks.map((task) => {
                    const isRunning =
                      task.status === 'running' || runningTaskIds.has(task.id);
                    const isCancelling = cancellingTaskIds.has(task.id);

                    const formatLastRun = (iso?: string | null) => {
                      if (!iso) return 'Never';
                      try {
                        const d = new Date(iso);
                        return isNaN(d.getTime()) ? iso : d.toLocaleString();
                      } catch {
                        return iso;
                      }
                    };

                    return (
                      <tr
                        key={task.id}
                        className="hover:bg-[#141414] transition-colors"
                      >
                        <td className="py-3.5 px-4 min-w-[200px]">
                          <span className="font-bold text-sm text-white block">
                            {task.name}
                          </span>
                          <span className="text-[11px] text-neutral-400 block mt-0.5 line-clamp-2">
                            {task.description}
                          </span>
                        </td>
                        <td className="py-3.5 px-4 whitespace-nowrap">
                          <span className="px-2 py-0.5 rounded-[2px] bg-[#181818] text-[10px] text-neutral-300 border border-[#282828]">
                            {task.interval}
                          </span>
                        </td>
                        <td className="py-3.5 px-4 whitespace-nowrap">
                          {task.status === 'running' ? (
                            <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[2px] bg-[#e5a00d]/10 text-[#e5a00d] border border-[#e5a00d]/30 font-bold text-[10px] uppercase">
                              <Loader2 className="h-3 w-3 animate-spin" />
                              Running
                            </span>
                          ) : task.status === 'failed' ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-[2px] bg-red-950/40 text-red-400 border border-red-800/40 font-bold text-[10px] uppercase">
                              Failed
                            </span>
                          ) : task.status === 'paused' ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-[2px] bg-yellow-950/40 text-yellow-400 border border-yellow-800/40 font-bold text-[10px] uppercase">
                              Paused
                            </span>
                          ) : (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-[2px] bg-neutral-800/80 text-neutral-400 border border-neutral-700 font-bold text-[10px] uppercase">
                              Idle
                            </span>
                          )}
                        </td>
                        <td className="py-3.5 px-4 whitespace-nowrap text-neutral-400">
                          {formatLastRun(task.last_run_at)}
                        </td>
                        <td className="py-3.5 px-4 whitespace-nowrap text-right">
                          <div className="flex items-center justify-end gap-2">
                            <TapeDeckButton
                              size="sm"
                              variant="amber"
                              disabled={isRunning || isCancelling}
                              onClick={() => handleRunTask(task.id)}
                              icon={
                                runningTaskIds.has(task.id) ? (
                                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                ) : (
                                  <Play className="h-3.5 w-3.5" />
                                )
                              }
                            >
                              Run Now
                            </TapeDeckButton>
                            {task.can_cancel && task.status === 'running' && (
                              <TapeDeckButton
                                size="sm"
                                variant="danger"
                                disabled={isCancelling}
                                onClick={() => handleCancelTask(task.id)}
                                icon={
                                  isCancelling ? (
                                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                  ) : (
                                    <Square className="h-3.5 w-3.5" />
                                  )
                                }
                              >
                                Cancel
                              </TapeDeckButton>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {tasks.length === 0 && (
              <div className="text-center py-12 text-neutral-500 font-mono text-sm">
                No scheduled background tasks registered.
              </div>
            )}
          </MachinedCard>
        </div>
      )}

      {/* System Status Subtab */}
      {!isLoading && activeTab === 'status' && (
        <div className="space-y-6">
        {isAdmin && isCore && <RequestPortalCard />}
        <MachinedCard className="p-6 max-w-2xl space-y-4">
          <h4 className="text-sm font-bold uppercase font-mono text-white">System Diagnostics</h4>
          <div className="divide-y divide-[#1f1f1f] text-xs font-mono">
            <div className="py-2.5 flex justify-between">
              <span className="text-neutral-400">TrackSeerr Version:</span>
              <span className="text-white">{systemStatus?.version || '1.0.0'}</span>
            </div>
            <div className="py-2.5 flex justify-between">
              <span className="text-neutral-400">Database Engine:</span>
              <span className="text-green-400">{systemStatus?.database_status || 'SQLite OK'}</span>
            </div>
            <div className="py-2.5 flex justify-between">
              <span className="text-neutral-400">Plex Server Connection:</span>
              <span className={systemStatus?.plex_connected ? 'text-green-400' : 'text-neutral-400'}>
                {systemStatus?.plex_connected ? 'Connected' : 'Configured'}
              </span>
            </div>
            <div className="py-2.5 flex justify-between">
              <span className="text-neutral-400">Lidarr Connection:</span>
              <span className={systemStatus?.lidarr_connected ? 'text-green-400' : 'text-neutral-500'}>
                {systemStatus?.lidarr_connected ? 'Connected' : 'Standalone Mode'}
              </span>
            </div>
          </div>
        </MachinedCard>
        </div>
      )}
    </div>
  );
};
