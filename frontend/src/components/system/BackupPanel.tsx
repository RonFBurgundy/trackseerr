import React, { useRef, useState } from 'react';
import {
  AlertTriangle,
  Archive,
  Database,
  Download,
  Loader2,
  RotateCcw,
  RotateCw,
  Settings,
  ShieldAlert,
  Trash2,
  Upload,
} from 'lucide-react';
import {
  FormField,
  MachinedCard,
  ObsidianModal,
  StatusMessage,
  TapeDeckButton,
  inputClass,
} from '@/components/ui';
import { formatBytes } from '@/components/lists/formatters';
import { getBackupDownloadUrl } from '@/services/backupService';
import { useBackups } from '@/hooks/useBackups';
import type { BackupItem } from '@/types/backup';

export interface BackupPanelProps {
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

const KIND_LABELS: Record<string, { label: string; className: string }> = {
  manual: {
    label: 'Manual',
    className: 'text-amber-400 bg-amber-950/40 border-amber-800/50',
  },
  scheduled: {
    label: 'Scheduled',
    className: 'text-emerald-400 bg-emerald-950/40 border-emerald-800/50',
  },
  pre_restore: {
    label: 'Pre-Restore',
    className: 'text-purple-400 bg-purple-950/40 border-purple-800/50',
  },
};

export const BackupPanel: React.FC<BackupPanelProps> = ({ onToast }) => {
  const {
    backups,
    settings,
    isLoading,
    isBackingUp,
    isRestoring,
    isUploading,
    isSavingSettings,
    isRestarting,
    error,
    fetchBackups,
    createBackup,
    deleteBackup,
    saveSettings,
    restore,
    uploadAndRestore,
  } = useBackups(onToast);

  const [deleteCandidate, setDeleteCandidate] = useState<BackupItem | null>(null);
  const [restoreCandidate, setRestoreCandidate] = useState<BackupItem | null>(null);
  const [showSettingsModal, setShowSettingsModal] = useState<boolean>(false);
  const [showUploadModal, setShowUploadModal] = useState<boolean>(false);
  const [selectedUploadFile, setSelectedUploadFile] = useState<File | null>(null);

  // Settings draft state
  const [retentionDraft, setRetentionDraft] = useState<number>(settings?.retention ?? 7);
  const [intervalDraft, setIntervalDraft] = useState<number>(settings?.interval_seconds ?? 604800);

  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleOpenSettings = (): void => {
    if (settings) {
      setRetentionDraft(settings.retention);
      setIntervalDraft(settings.interval_seconds ?? 604800);
    }
    setShowSettingsModal(true);
  };

  const handleSaveSettings = async (): Promise<void> => {
    const success = await saveSettings({
      retention: retentionDraft,
      interval_seconds: intervalDraft,
    });
    if (success) {
      setShowSettingsModal(false);
    }
  };

  const handleConfirmDelete = async (): Promise<void> => {
    if (!deleteCandidate) return;
    const success = await deleteBackup(deleteCandidate.name);
    if (success) {
      setDeleteCandidate(null);
    }
  };

  const handleConfirmRestore = async (): Promise<void> => {
    if (!restoreCandidate) return;
    const name = restoreCandidate.name;
    setRestoreCandidate(null);
    await restore(name);
  };

  const handleConfirmUploadRestore = async (): Promise<void> => {
    if (!selectedUploadFile) return;
    const file = selectedUploadFile;
    setShowUploadModal(false);
    setSelectedUploadFile(null);
    await uploadAndRestore(file);
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>): void => {
    const files = e.target.files;
    if (files && files.length > 0) {
      setSelectedUploadFile(files[0]);
    }
  };

  const formatDate = (isoString?: string | null): string => {
    if (!isoString) return 'Unknown date';
    try {
      const d = new Date(isoString);
      return Number.isNaN(d.getTime()) ? isoString : d.toLocaleString();
    } catch {
      return isoString;
    }
  };

  return (
    <div className="space-y-6">
      {/* Restarting State Modal Overlay */}
      {isRestarting && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 backdrop-blur-sm p-4">
          <div className="w-full max-w-md p-6 bg-[#121212] border border-[#2a2a2a] rounded-[4px] shadow-2xl text-center space-y-4">
            <Loader2 className="h-10 w-10 animate-spin text-[#e5a00d] mx-auto" />
            <h3 className="text-lg font-bold text-white uppercase tracking-wider font-mono">
              Restarting TrackSeerr
            </h3>
            <p className="text-xs text-neutral-300 font-mono leading-relaxed">
              Applying the restored database snapshot and waiting for the server to come back online.
              This page will reload automatically.
            </p>
          </div>
        </div>
      )}

      {/* Main Backups Card */}
      <MachinedCard className="p-4 sm:p-6 space-y-5">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 border-b border-[#222222] pb-4">
          <div>
            <h4 className="text-sm font-bold uppercase font-mono text-white flex items-center gap-2">
              <Database className="h-4 w-4 text-[#e5a00d]" />
              Database Backups &amp; Restore
            </h4>
            <p className="text-xs text-neutral-400 font-mono mt-1">
              On-demand snapshots, scheduled retention pruning, and atomic database restores.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <TapeDeckButton
              size="sm"
              variant="default"
              onClick={() => void fetchBackups()}
              disabled={isLoading}
              icon={isLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCw className="h-3.5 w-3.5" />}
            >
              Refresh
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              variant="default"
              onClick={handleOpenSettings}
              icon={<Settings className="h-3.5 w-3.5" />}
            >
              Settings
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              variant="default"
              onClick={() => setShowUploadModal(true)}
              disabled={isUploading || isRestoring}
              icon={<Upload className="h-3.5 w-3.5" />}
            >
              Restore from file
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              variant="amber"
              onClick={() => void createBackup()}
              disabled={isBackingUp || isLoading}
              icon={isBackingUp ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Archive className="h-3.5 w-3.5" />}
            >
              Back up now
            </TapeDeckButton>
          </div>
        </div>

        {/* Security Warning Notice */}
        <div className="flex items-start gap-2.5 p-3 rounded-[3px] bg-[#161616] border border-[#2a2a2a] text-xs font-mono text-neutral-300">
          <ShieldAlert className="h-4 w-4 text-[#e5a00d] shrink-0 mt-0.5" />
          <span>
            Database backups contain sensitive server configuration, API credentials, and password hashes.
            Downloaded backup files should be stored in a secure location.
          </span>
        </div>

        {error && <StatusMessage variant="error">{error}</StatusMessage>}

        {/* Backup List Table */}
        {isLoading && backups.length === 0 ? (
          <div className="py-12 flex justify-center text-neutral-400 font-mono text-xs">
            <Loader2 className="h-5 w-5 animate-spin text-[#e5a00d] mr-2" />
            Loading backups...
          </div>
        ) : backups.length === 0 ? (
          <div className="py-10 text-center text-neutral-400 font-mono text-xs border border-dashed border-[#222222] rounded-[4px]">
            No database backups found. Click &quot;Back up now&quot; to generate an initial snapshot.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left font-mono text-xs divide-y divide-[#1f1f1f]">
              <thead>
                <tr className="text-neutral-400 border-b border-[#222222]">
                  <th className="py-2.5 px-3 uppercase">Created</th>
                  <th className="py-2.5 px-3 uppercase">Type</th>
                  <th className="py-2.5 px-3 uppercase">Size</th>
                  <th className="py-2.5 px-3 uppercase">Version</th>
                  <th className="py-2.5 px-3 uppercase text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#181818]">
                {backups.map((b) => {
                  const kindMeta = KIND_LABELS[b.kind || 'manual'] || {
                    label: b.kind || 'Unknown',
                    className: 'text-neutral-300 bg-neutral-900 border-neutral-700',
                  };
                  return (
                    <tr key={b.name} className="hover:bg-[#161616] transition-colors">
                      <td className="py-3 px-3">
                        <div className="text-white font-medium">{formatDate(b.created_at)}</div>
                        <div className="text-[10px] text-neutral-400 truncate max-w-xs sm:max-w-md">
                          {b.name}
                        </div>
                        {b.error && (
                          <div className="text-red-400 text-[10px] flex items-center gap-1 mt-1">
                            <AlertTriangle className="h-3 w-3 shrink-0" />
                            {b.error}
                          </div>
                        )}
                      </td>
                      <td className="py-3 px-3">
                        <span
                          className={`inline-block px-2 py-0.5 text-[10px] font-semibold border rounded-[3px] uppercase ${kindMeta.className}`}
                        >
                          {kindMeta.label}
                        </span>
                      </td>
                      <td className="py-3 px-3 text-neutral-300">{formatBytes(b.size)}</td>
                      <td className="py-3 px-3 text-neutral-400 text-[11px]">
                        {b.app_version ? `v${b.app_version}` : 'Unknown'}
                        {b.schema_version !== null && b.schema_version !== undefined && (
                          <span className="text-neutral-400 ml-1">
                            (schema {b.schema_version})
                          </span>
                        )}
                      </td>
                      <td className="py-3 px-3 text-right">
                        <div className="flex items-center justify-end gap-1.5">
                          <a
                            href={getBackupDownloadUrl(b.name)}
                            download={b.name}
                            className="p-1.5 rounded-[3px] text-neutral-300 hover:text-white hover:bg-[#222222] transition-colors"
                            title="Download backup"
                            aria-label={`Download backup ${b.name}`}
                          >
                            <Download className="h-3.5 w-3.5" />
                          </a>
                          <button
                            type="button"
                            onClick={() => setRestoreCandidate(b)}
                            disabled={Boolean(b.error) || isRestoring}
                            className="p-1.5 rounded-[3px] text-amber-400 hover:text-amber-300 hover:bg-[#222222] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                            title="Restore database from this backup"
                            aria-label={`Restore backup ${b.name}`}
                          >
                            <RotateCcw className="h-3.5 w-3.5" />
                          </button>
                          <button
                            type="button"
                            onClick={() => setDeleteCandidate(b)}
                            className="p-1.5 rounded-[3px] text-red-400 hover:text-red-300 hover:bg-[#222222] transition-colors"
                            title="Delete backup"
                            aria-label={`Delete backup ${b.name}`}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </MachinedCard>

      {/* Settings Modal */}
      <ObsidianModal
        isOpen={showSettingsModal}
        onClose={() => setShowSettingsModal(false)}
        title="Backup Settings"
        subtitle="Retention limits and scheduled backup interval"
        maxWidth="sm:max-w-lg"
        footer={
          <>
            <TapeDeckButton size="sm" onClick={() => setShowSettingsModal(false)}>
              Cancel
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              variant="amber"
              onClick={() => void handleSaveSettings()}
              disabled={isSavingSettings}
              icon={isSavingSettings ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
            >
              Save Settings
            </TapeDeckButton>
          </>
        }
      >
        <div className="space-y-4 font-mono text-xs">
          <FormField
            label="Scheduled Backup Retention"
            name="retention"
            hint="Number of recent scheduled backups to retain. Manual and pre-restore backups are never deleted automatically."
          >
            <input
              type="number"
              min={1}
              max={365}
              value={retentionDraft}
              onChange={(e) => setRetentionDraft(Math.max(1, parseInt(e.target.value, 10) || 1))}
              className={inputClass}
            />
          </FormField>

          <FormField
            label="Backup Frequency"
            name="interval"
            hint="How frequently automated scheduled database snapshots are taken."
          >
            <select
              value={intervalDraft}
              onChange={(e) => setIntervalDraft(parseInt(e.target.value, 10))}
              className={inputClass}
            >
              <option value={86400}>Daily (Every 24 hours)</option>
              <option value={259200}>Every 3 days</option>
              <option value={604800}>Weekly (Every 7 days)</option>
              <option value={1209600}>Biweekly (Every 14 days)</option>
            </select>
          </FormField>
        </div>
      </ObsidianModal>

      {/* Confirm Restore Modal */}
      <ObsidianModal
        isOpen={restoreCandidate !== null}
        onClose={() => setRestoreCandidate(null)}
        title="Confirm Database Restore"
        subtitle="This action will replace the active database"
        maxWidth="sm:max-w-md"
        footer={
          <>
            <TapeDeckButton size="sm" onClick={() => setRestoreCandidate(null)}>
              Cancel
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              variant="amber"
              onClick={() => void handleConfirmRestore()}
              disabled={isRestoring}
              icon={isRestoring ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
            >
              Confirm &amp; Restart
            </TapeDeckButton>
          </>
        }
      >
        <div className="space-y-3 font-mono text-xs text-neutral-300">
          <p>
            You are about to restore database backup{' '}
            <span className="text-white font-bold">{restoreCandidate?.name}</span>.
          </p>
          <div className="p-3 bg-amber-950/30 border border-amber-800/40 rounded-[3px] text-amber-300 text-[11px] leading-relaxed">
            <strong>Important:</strong> Restoring replaces all current database records.
            TrackSeerr will automatically capture a safety snapshot (<span className="text-white">pre_restore</span>)
            of the live database before replacing it. TrackSeerr will restart immediately after staging.
          </div>
        </div>
      </ObsidianModal>

      {/* Confirm Delete Modal */}
      <ObsidianModal
        isOpen={deleteCandidate !== null}
        onClose={() => setDeleteCandidate(null)}
        title="Delete Backup"
        subtitle="Permanently remove backup archive"
        maxWidth="sm:max-w-md"
        footer={
          <>
            <TapeDeckButton size="sm" onClick={() => setDeleteCandidate(null)}>
              Cancel
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              variant="danger"
              onClick={() => void handleConfirmDelete()}
              icon={<Trash2 className="h-3.5 w-3.5" />}
            >
              Delete
            </TapeDeckButton>
          </>
        }
      >
        <p className="font-mono text-xs text-neutral-300">
          Are you sure you want to permanently delete backup{' '}
          <span className="text-white font-bold">{deleteCandidate?.name}</span>? This action cannot be undone.
        </p>
      </ObsidianModal>

      {/* Restore from File Upload Modal */}
      <ObsidianModal
        isOpen={showUploadModal}
        onClose={() => {
          setShowUploadModal(false);
          setSelectedUploadFile(null);
        }}
        title="Restore From Backup File"
        subtitle="Upload a zip archive to restore"
        maxWidth="sm:max-w-md"
        footer={
          <>
            <TapeDeckButton
              size="sm"
              onClick={() => {
                setShowUploadModal(false);
                setSelectedUploadFile(null);
              }}
            >
              Cancel
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              variant="amber"
              onClick={() => void handleConfirmUploadRestore()}
              disabled={!selectedUploadFile || isUploading}
              icon={isUploading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}
            >
              Upload &amp; Restore
            </TapeDeckButton>
          </>
        }
      >
        <div className="space-y-4 font-mono text-xs text-neutral-300">
          <input
            type="file"
            ref={fileInputRef}
            onChange={handleFileChange}
            accept=".zip"
            className="hidden"
          />

          <div
            onClick={() => fileInputRef.current?.click()}
            className="p-6 border-2 border-dashed border-[#2a2a2a] hover:border-[#e5a00d] rounded-[4px] text-center cursor-pointer transition-colors bg-[#141414]"
          >
            <Upload className="h-6 w-6 text-[#e5a00d] mx-auto mb-2" />
            {selectedUploadFile ? (
              <div>
                <p className="text-white font-bold">{selectedUploadFile.name}</p>
                <p className="text-neutral-400 text-[11px] mt-0.5">
                  {formatBytes(selectedUploadFile.size)}
                </p>
              </div>
            ) : (
              <div>
                <p className="text-white font-medium">Click to select backup zip file</p>
                <p className="text-neutral-400 text-[10px] mt-1">
                  Must be a valid TrackSeerr backup containing sync_db.sqlite and manifest.json
                </p>
              </div>
            )}
          </div>

          <div className="p-3 bg-amber-950/30 border border-amber-800/40 rounded-[3px] text-amber-300 text-[11px] leading-relaxed">
            <strong>Warning:</strong> Uploading and restoring will replace the current database after validation.
            A safety pre-restore backup will be created automatically. TrackSeerr will restart immediately.
          </div>
        </div>
      </ObsidianModal>
    </div>
  );
};
