import React, { useState } from 'react';
import { Ban, Loader2 } from 'lucide-react';
import { ConfirmDangerButton } from '@/components/ui';
import { errorMessage } from '@/services/apiClient';
import { rejectCurrentRelease } from '@/services/libraryService';

export interface RejectCurrentButtonProps {
  entity: 'album' | 'track';
  entityId: string;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
  /** Fires after the server accepted the rejection (blocklisted and/or search queued). */
  onDone?: () => void;
}

const HELP =
  'Blocklists the release behind the current files and searches for another. The current files stay until the replacement imports.';

/** Two-step "Reject current release": blocklists the release behind the item's files and searches for another. */
export const RejectCurrentButton: React.FC<RejectCurrentButtonProps> = ({ entity, entityId, onToast, onDone }) => {
  const [busy, setBusy] = useState<boolean>(false);
  const run = (): void => {
    setBusy(true);
    rejectCurrentRelease(entity, entityId)
      .then((res) => {
        onToast(res.message);
        onDone?.();
      })
      .catch((err: unknown) => onToast(errorMessage(err, 'Could not reject the current release'), 'error'))
      .finally(() => setBusy(false));
  };
  return (
    <span className="contents" title={HELP}>
      <ConfirmDangerButton
        icon={busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Ban className="h-3.5 w-3.5" />}
        idleLabel="Reject current release"
        ariaLabel={`Reject current release. ${HELP}`}
        confirmLabel="Blocklist & search"
        collapseLabel
        disabled={busy}
        onConfirm={run}
      />
    </span>
  );
};
