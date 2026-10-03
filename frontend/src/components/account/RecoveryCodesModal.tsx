import React, { useState } from 'react';
import { CopyBox, ObsidianModal, StatusMessage, TapeDeckButton } from '@/components/ui';

export interface RecoveryCodesModalProps {
  codes: string[] | null;
  onDone: () => void;
}

/** Shows freshly issued recovery codes exactly once; cannot be closed until acknowledged. */
export const RecoveryCodesModal: React.FC<RecoveryCodesModalProps> = ({ codes, onDone }) => {
  const [saved, setSaved] = useState<boolean>(false);
  const [nag, setNag] = useState<boolean>(false);

  const finish = () => {
    if (!saved) {
      setNag(true);
      return;
    }
    setSaved(false);
    setNag(false);
    onDone();
  };

  return (
    <ObsidianModal
      isOpen={codes !== null}
      onClose={finish}
      title="Recovery codes"
      subtitle="Shown only once"
      maxWidth="sm:max-w-lg"
      footer={
        <TapeDeckButton variant="amber" onClick={finish} disabled={!saved}>
          Done
        </TapeDeckButton>
      }
    >
      <div className="space-y-4">
        <p className="text-xs font-mono text-[var(--text-secondary)]">
          Each code works once if you lose access to your authenticator app. Store them somewhere
          safe. They will not be shown again.
        </p>
        <CopyBox value={(codes ?? []).join('\n')} multiline />
        <label className="flex items-center gap-3 min-h-[44px] cursor-pointer text-xs font-mono">
          <input
            type="checkbox"
            checked={saved}
            onChange={(e) => {
              setSaved(e.target.checked);
              setNag(false);
            }}
            className="h-4 w-4 accent-[var(--accent-amber)]"
          />
          I've saved them
        </label>
        {nag && <StatusMessage variant="error">Confirm that you have saved your codes first.</StatusMessage>}
      </div>
    </ObsidianModal>
  );
};
