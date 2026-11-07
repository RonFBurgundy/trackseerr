import React, { useId, useState } from 'react';
import { ConfirmDialog } from '@/components/ui';
import { createExclusion, type ExclusionCreate, type ExclusionCreated } from '@/services/exclusionsService';
import { errorMessage } from '@/services/apiClient';

export interface ExcludeConfirmProps {
  isOpen: boolean;
  scope: 'artist' | 'album' | 'track';
  name: string;
  payload: Omit<ExclusionCreate, 'reason' | 'scope'>;
  onClose: () => void;
  onSuccess?: (res: ExclusionCreated) => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

export const ExcludeConfirm: React.FC<ExcludeConfirmProps> = ({
  isOpen,
  scope,
  name,
  payload,
  onClose,
  onSuccess,
  onToast,
}) => {
  const reasonInputId = useId();
  const [reason, setReason] = useState<string>('');
  const [busy, setBusy] = useState<boolean>(false);

  const handleConfirm = async (): Promise<void> => {
    setBusy(true);
    try {
      const res = await createExclusion({
        ...payload,
        scope,
        reason: reason.trim() || undefined,
      });
      const resolvedName = res.item.title || res.item.artist_name || name;
      const toastMsg =
        res.unmonitored > 0
          ? `Excluded "${resolvedName}", unmonitored ${res.unmonitored}`
          : `Excluded "${resolvedName}"`;
      onToast(toastMsg, 'ok');
      setReason('');
      onSuccess?.(res);
      onClose();
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to exclude item'), 'error');
    } finally {
      setBusy(false);
    }
  };

  const handleCancel = (): void => {
    if (busy) return;
    setReason('');
    onClose();
  };

  return (
    <ConfirmDialog
      isOpen={isOpen}
      title={`Exclude ${scope}?`}
      confirmLabel="Exclude"
      onConfirm={() => void handleConfirm()}
      onCancel={handleCancel}
      busy={busy}
    >
      <div className="space-y-4">
        <p className="text-xs sm:text-sm font-mono text-neutral-300">
          TrackSeerr will never search for, request or add &ldquo;{name}&rdquo; again, including from playlists and lists. It is unmonitored now. You can remove it later in Activity &rarr; Exclusions.
        </p>
        <div className="space-y-1.5">
          <label htmlFor={reasonInputId} className="block text-xs font-mono uppercase tracking-wider text-neutral-400">
            Reason (optional)
          </label>
          <input
            id={reasonInputId}
            name="exclusion_reason"
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={busy}
            placeholder="e.g. Disliked artist, low quality release"
            className="w-full h-9 sm:h-8 px-2.5 text-[13px] font-mono bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] text-neutral-200 placeholder:text-neutral-600 focus:border-[#e5a00d] focus:outline-none transition-colors"
          />
        </div>
      </div>
    </ConfirmDialog>
  );
};
