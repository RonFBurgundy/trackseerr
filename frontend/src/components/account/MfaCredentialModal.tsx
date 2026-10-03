import React, { useState } from 'react';
import { Loader2 } from 'lucide-react';
import { FormField, ObsidianModal, StatusMessage, TapeDeckButton, inputClass } from '@/components/ui';

export interface MfaCredentialModalProps {
  isOpen: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  danger?: boolean;
  onClose: () => void;
  /** Resolves to an error message, or null on success (modal then closes). */
  onSubmit: (password: string, code: string) => Promise<string | null>;
}

export const MfaCredentialModal: React.FC<MfaCredentialModalProps> = ({
  isOpen,
  title,
  description,
  confirmLabel,
  danger = false,
  onClose,
  onSubmit,
}) => {
  const [password, setPassword] = useState<string>('');
  const [code, setCode] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [isBusy, setIsBusy] = useState<boolean>(false);

  const close = () => {
    setPassword('');
    setCode('');
    setError(null);
    onClose();
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isBusy) return;
    setIsBusy(true);
    setError(null);
    try {
      const err = await onSubmit(password, code);
      if (err) setError(err);
      else close();
    } finally {
      setIsBusy(false);
    }
  };

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={close}
      title={title}
      maxWidth="sm:max-w-md"
      footer={
        <>
          <TapeDeckButton type="button" onClick={close}>
            Cancel
          </TapeDeckButton>
          <TapeDeckButton
            type="submit"
            form="mfa-credential-form"
            variant={danger ? 'danger' : 'amber'}
            disabled={isBusy || !password || !code.trim()}
            icon={isBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
          >
            {confirmLabel}
          </TapeDeckButton>
        </>
      }
    >
      <form id="mfa-credential-form" onSubmit={submit} className="space-y-4">
        <p className="text-xs font-mono text-[var(--text-secondary)]">{description}</p>
        <FormField label="Password" htmlFor="mfa-cred-pw">
          <input
            id="mfa-cred-pw"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={inputClass}
          />
        </FormField>
        <FormField label="Authentication code" htmlFor="mfa-cred-code">
          <input
            id="mfa-cred-code"
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            className={inputClass}
          />
        </FormField>
        {error && <StatusMessage variant="error">{error}</StatusMessage>}
      </form>
    </ObsidianModal>
  );
};
