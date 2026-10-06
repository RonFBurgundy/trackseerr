import React, { useId, useState } from 'react';
import { Loader2, Trash2 } from 'lucide-react';
import { ConfirmDialog, TactileSwitch, TapeDeckButton } from '@/components/ui';
import type { MediaManagementSettings } from '@/types/models';
import { ApiError } from '@/services/apiClient';
import { emptyRecycleBin } from '@/services/settingsService';
import { inputClass, labelClass } from './formClasses';

export interface RecycleBinSectionProps {
  settings: MediaManagementSettings | null;
  onChange: React.Dispatch<React.SetStateAction<MediaManagementSettings | null>>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
  /** Non-blocking notes returned by the last save. */
  warnings: string[];
}

/** Recycle bin, permanent-delete opt-in and quarantine folder (the destinations for replaced and rejected files). */
export const RecycleBinSection: React.FC<RecycleBinSectionProps> = React.memo(({ settings, onChange, onToast, warnings }) => {
  const recycleId = useId();
  const daysId = useId();
  const quarantineId = useId();
  const [confirmDelete, setConfirmDelete] = useState<boolean>(false);
  const [confirmEmpty, setConfirmEmpty] = useState<boolean>(false);
  const [emptying, setEmptying] = useState<boolean>(false);
  const permanent = settings?.recycle_bin_permanent_delete ?? false;

  const handleEmpty = async (): Promise<void> => {
    setEmptying(true);
    try {
      const res = await emptyRecycleBin();
      setConfirmEmpty(false);
      if (res.skipped_reason) onToast(`Recycle bin not emptied: ${res.skipped_reason}`, 'error');
      else if (res.errors.length > 0) onToast(`Removed ${res.removed} item(s); ${res.errors.length} could not be removed`, 'error');
      else onToast(`Recycle bin emptied (${res.removed} item${res.removed === 1 ? '' : 's'} removed)`);
    } catch (err) {
      onToast(err instanceof ApiError ? err.message : 'Failed to empty the recycle bin', 'error');
    } finally {
      setEmptying(false);
    }
  };

  return (
    <div className="pt-2 border-t border-[#1f1f1f] space-y-3">
      <div>
        <h5 className="text-xs font-bold uppercase font-mono text-white">Recycle bin &amp; quarantine</h5>
        <p className="text-[11px] text-neutral-400 font-mono mt-0.5">
          When an upgrade replaces a file, the old one is moved here (never copied or deleted), so a hardlinked
          seeding torrent keeps working. Both folders must be on the same filesystem as your library.
        </p>
      </div>

      <div>
        <label htmlFor={recycleId} className={labelClass}>Recycle bin folder</label>
        <input
          id={recycleId}
          name="recycle_bin_path"
          type="text"
          value={settings?.recycle_bin_path ?? ''}
          onChange={(e) => onChange((prev) => (prev ? { ...prev, recycle_bin_path: e.target.value } : null))}
          placeholder={settings?.effective_recycle_bin_path ?? '<library>/.trackseerr-recycle'}
          className={`${inputClass} font-mono`}
        />
        <p className="mt-1.5 text-[11px] font-mono text-neutral-500">
          Leave empty for the default inside your library. Must be an absolute path, outside every download folder.
        </p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label htmlFor={daysId} className={labelClass}>Delete recycled files after (days)</label>
          <input
            id={daysId}
            name="recycle_bin_cleanup_days"
            type="number"
            min={0}
            step={1}
            value={settings?.recycle_bin_cleanup_days ?? 30}
            onChange={(e) => {
              const n = Number.parseInt(e.target.value, 10);
              onChange((prev) => (prev ? { ...prev, recycle_bin_cleanup_days: Number.isFinite(n) && n >= 0 ? n : 0 } : null));
            }}
            className={`${inputClass} font-mono`}
          />
          <p className="mt-1.5 text-[11px] font-mono text-neutral-500">0 keeps recycled files until you empty the bin.</p>
        </div>
        <div className="flex flex-col justify-end">
          <TapeDeckButton
            type="button"
            size="sm"
            onClick={() => setConfirmEmpty(true)}
            icon={<Trash2 className="h-4 w-4" />}
          >
            Empty recycle bin
          </TapeDeckButton>
        </div>
      </div>

      <div>
        <div className="flex items-center justify-between gap-3">
          <span className="text-xs font-mono text-neutral-300">Delete replaced files permanently instead</span>
          <TactileSwitch
            checked={permanent}
            onChange={(val) => {
              if (val) setConfirmDelete(true);
              else onChange((prev) => (prev ? { ...prev, recycle_bin_permanent_delete: false } : null));
            }}
            label="Delete replaced files permanently instead"
            name="recycle_bin_permanent_delete"
          />
        </div>
        <p className="mt-1.5 text-[11px] font-mono text-neutral-500">
          Off by default. An empty recycle bin folder never means &ldquo;delete&rdquo;: files are only deleted when this is on.
        </p>
      </div>

      <div>
        <label htmlFor={quarantineId} className={labelClass}>Quarantine folder</label>
        <input
          id={quarantineId}
          name="quarantine_folder_path"
          type="text"
          value={settings?.quarantine_folder_path ?? ''}
          onChange={(e) => onChange((prev) => (prev ? { ...prev, quarantine_folder_path: e.target.value } : null))}
          placeholder={settings?.effective_quarantine_folder_path ?? '<library>/.trackseerr-quarantine'}
          className={`${inputClass} font-mono`}
        />
        <p className="mt-1.5 text-[11px] font-mono text-neutral-500">
          Downloads rejected by the import security check. Torrent files are copied here and keep seeding; usenet and
          Soulseek files are moved.
        </p>
      </div>

      {warnings.length > 0 && (
        <ul role="status" className="space-y-1">
          {warnings.map((w) => (
            <li
              key={w}
              className="px-2 py-1.5 rounded-[3px] border border-[#e5a00d]/50 bg-[#e5a00d]/10 text-[11px] font-mono text-[#e5a00d]"
            >
              {w}
            </li>
          ))}
        </ul>
      )}

      <ConfirmDialog
        isOpen={confirmDelete}
        title="Delete replaced files permanently?"
        confirmLabel="Delete permanently"
        onConfirm={() => {
          onChange((prev) => (prev ? { ...prev, recycle_bin_permanent_delete: true } : null));
          setConfirmDelete(false);
        }}
        onCancel={() => setConfirmDelete(false)}
      >
        <p>
          After the next save, files replaced by an upgrade are deleted instead of moved to the recycle bin and cannot be
          recovered. Hardlinked seeding copies are not affected.
        </p>
      </ConfirmDialog>
      <ConfirmDialog
        isOpen={confirmEmpty}
        title="Empty the recycle bin?"
        confirmLabel="Empty recycle bin"
        onConfirm={() => void handleEmpty()}
        onCancel={() => setConfirmEmpty(false)}
        busy={emptying}
      >
        <p>
          {emptying && <Loader2 className="inline h-3 w-3 animate-spin mr-1" />}
          Everything in the recycle bin is permanently deleted. Files in your library and download folders are not touched.
        </p>
      </ConfirmDialog>
    </div>
  );
});
RecycleBinSection.displayName = 'RecycleBinSection';
