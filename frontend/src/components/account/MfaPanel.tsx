import React, { useState } from 'react';
import { Loader2, ShieldCheck } from 'lucide-react';
import {
  CopyBox,
  FormField,
  MachinedCard,
  StatusMessage,
  TapeDeckButton,
  inputClass,
} from '@/components/ui';
import type { UseAccountReturn } from '@/hooks/useAccount';
import { MfaCredentialModal } from './MfaCredentialModal';
import { RecoveryCodesModal } from './RecoveryCodesModal';

export interface MfaPanelProps {
  accountHook: UseAccountReturn;
  /** True when sign-in is blocked until the user finishes enrolling. */
  enrollmentBlocking?: boolean;
}

/** Base32 secret split into groups of 4 for easy manual entry. */
export function groupSecret(secret: string): string {
  return (secret.match(/.{1,4}/g) ?? []).join(' ');
}

export const MfaPanel: React.FC<MfaPanelProps> = ({ accountHook, enrollmentBlocking = false }) => {
  const { account, mfaSetup, recoveryCodes } = accountHook;
  const [code, setCode] = useState<string>('');
  const [setupPassword, setSetupPassword] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [isBusy, setIsBusy] = useState<boolean>(false);
  const [modal, setModal] = useState<'disable' | 'regenerate' | null>(null);

  if (!account) return null;

  const begin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!setupPassword) return;
    setIsBusy(true);
    setError(null);
    try {
      const err = await accountHook.beginMfaSetup(setupPassword);
      setError(err);
      if (!err) setSetupPassword('');
    } finally {
      setIsBusy(false);
    }
  };

  const confirm = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsBusy(true);
    setError(null);
    try {
      const err = await accountHook.confirmMfaSetup(code);
      setError(err);
      if (!err) setCode('');
    } finally {
      setIsBusy(false);
    }
  };

  return (
    <MachinedCard className="p-4 sm:p-6 space-y-4">
      <div className="flex items-center gap-2">
        <ShieldCheck className="h-4 w-4 text-[var(--accent-amber)]" />
        <h3 className="text-sm font-bold uppercase tracking-wider">Two-factor authentication</h3>
        <span
          className={`ml-auto text-[10px] font-mono px-1.5 py-0.5 rounded-[2px] border ${
            account.mfa_enabled
              ? 'border-[var(--status-success)] text-[var(--status-success)]'
              : 'border-[var(--border-default)] text-[var(--text-muted)]'
          }`}
        >
          {account.mfa_enabled ? 'ENABLED' : 'OFF'}
        </span>
      </div>

      {enrollmentBlocking && !account.mfa_enabled && (
        <StatusMessage variant="info">
          An administrator requires two-factor authentication. Set it up to continue using
          TrackSeerr.
        </StatusMessage>
      )}
      {error && <StatusMessage variant="error">{error}</StatusMessage>}

      {!account.mfa_enabled && !mfaSetup && (
        <form onSubmit={begin} className="space-y-3">
          <p className="text-xs font-mono text-[var(--text-secondary)]">
            Protect your account with a code from an authenticator app. Confirm your password to
            begin.
          </p>
          <FormField label="Password" htmlFor="mfa-setup-password">
            <input
              id="mfa-setup-password"
              type="password"
              autoComplete="current-password"
              value={setupPassword}
              onChange={(e) => setSetupPassword(e.target.value)}
              className={`${inputClass} sm:max-w-[16rem]`}
            />
          </FormField>
          <TapeDeckButton
            type="submit"
            variant="amber"
            disabled={isBusy || !setupPassword}
            icon={isBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
          >
            Set up MFA
          </TapeDeckButton>
        </form>
      )}

      {mfaSetup && (
        <form onSubmit={confirm} className="space-y-4">
          <p className="text-xs font-mono text-[var(--text-secondary)]">
            Add this account to your authenticator app by entering the key below (or by pasting the
            otpauth link), then type the 6-digit code it shows. The key is only valid for 10
            minutes.
          </p>
          <CopyBox label="Secret key" value={groupSecret(mfaSetup.secret)} />
          <CopyBox label="otpauth link" value={mfaSetup.otpauth_uri} />
          <FormField label="Confirmation code" htmlFor="mfa-confirm-code">
            <input
              id="mfa-confirm-code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className={`${inputClass} sm:max-w-[12rem]`}
            />
          </FormField>
          <div className="flex flex-wrap gap-2">
            <TapeDeckButton
              type="submit"
              variant="amber"
              disabled={isBusy || !code.trim()}
              icon={isBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
            >
              Confirm and enable
            </TapeDeckButton>
            <TapeDeckButton
              type="button"
              onClick={() => {
                accountHook.cancelMfaSetup();
                setCode('');
                setError(null);
              }}
            >
              Cancel
            </TapeDeckButton>
          </div>
        </form>
      )}

      {account.mfa_enabled && (
        <div className="space-y-3">
          <p className="text-xs font-mono text-[var(--text-secondary)]">
            Recovery codes remaining: {account.recovery_codes_remaining}
          </p>
          <div className="flex flex-wrap gap-2">
            <TapeDeckButton onClick={() => setModal('regenerate')}>Regenerate codes</TapeDeckButton>
            <TapeDeckButton
              variant="danger"
              onClick={() => setModal('disable')}
              disabled={account.mfa_required}
              title={account.mfa_required ? 'An administrator requires MFA' : undefined}
            >
              Disable MFA
            </TapeDeckButton>
          </div>
          {account.mfa_required && (
            <p className="text-[11px] font-mono text-[var(--text-muted)]">
              MFA cannot be disabled because an administrator requires it.
            </p>
          )}
        </div>
      )}

      <MfaCredentialModal
        isOpen={modal === 'disable'}
        title="Disable MFA"
        description="Confirm your password and a current code to turn off two-factor authentication."
        confirmLabel="Disable MFA"
        danger
        onClose={() => setModal(null)}
        onSubmit={accountHook.disableMfa}
      />
      <MfaCredentialModal
        isOpen={modal === 'regenerate'}
        title="Regenerate recovery codes"
        description="This invalidates all existing recovery codes and issues 10 new ones."
        confirmLabel="Regenerate"
        onClose={() => setModal(null)}
        onSubmit={accountHook.regenerateRecoveryCodes}
      />
      <RecoveryCodesModal codes={recoveryCodes} onDone={accountHook.dismissRecoveryCodes} />
    </MachinedCard>
  );
};
