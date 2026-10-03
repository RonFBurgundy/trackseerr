import React, { useEffect, useState } from 'react';
import { Loader2, KeyRound } from 'lucide-react';
import { MachinedCard, TapeDeckButton, StatusMessage } from '@/components/ui';
import type { InviteInfo } from '@/types/account';
import {
  INVALID_LINK_MESSAGE,
  getInvite,
  redeemInvite,
} from '@/services/localAuthService';
import { PasswordSetupFields } from './PasswordSetupFields';
import { checkPassword } from './passwordPolicy';

export interface InvitePageProps {
  token: string;
}

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; info: InviteInfo }
  | { kind: 'invalid' }
  | { kind: 'error'; message: string };

function formatExpiry(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** Standalone page for /invite/:token. Works without a session. */
export const InvitePage: React.FC<InvitePageProps> = ({ token }) => {
  const [state, setState] = useState<LoadState>({ kind: 'loading' });
  const [password, setPassword] = useState<string>('');
  const [confirm, setConfirm] = useState<string>('');
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [done, setDone] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    void getInvite(token).then((res) => {
      if (cancelled) return;
      if (res.kind === 'ok') setState({ kind: 'ready', info: res.info });
      else if (res.kind === 'invalid') setState({ kind: 'invalid' });
      else setState({ kind: 'error', message: res.message });
    });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const goToSignIn = () => {
    window.location.assign('/');
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (state.kind !== 'ready' || isSubmitting) return;
    if (!checkPassword(password, confirm, state.info.username).allOk) return;
    setIsSubmitting(true);
    setSubmitError(null);
    try {
      const res = await redeemInvite(token, password);
      if (res.kind === 'ok') {
        setPassword('');
        setConfirm('');
        setDone(true);
      } else if (res.kind === 'invalid') {
        setState({ kind: 'invalid' });
      } else {
        setSubmitError(res.message);
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  let body: React.ReactNode;
  if (state.kind === 'loading') {
    body = (
      <div className="flex justify-center py-10">
        <Loader2 className="h-8 w-8 text-[var(--accent-amber)] animate-spin" />
      </div>
    );
  } else if (state.kind === 'invalid') {
    body = (
      <div className="space-y-4 text-center">
        <StatusMessage variant="error">{INVALID_LINK_MESSAGE}</StatusMessage>
        <p className="text-xs font-mono text-[var(--text-secondary)]">
          Ask an administrator for a new link.
        </p>
        <TapeDeckButton size="md" className="w-full" onClick={goToSignIn}>
          Go to sign in
        </TapeDeckButton>
      </div>
    );
  } else if (state.kind === 'error') {
    body = <StatusMessage variant="error">{state.message}</StatusMessage>;
  } else if (done) {
    body = (
      <div className="space-y-4 text-center">
        <StatusMessage variant="success">
          Your password has been set. You can now sign in.
        </StatusMessage>
        <TapeDeckButton size="lg" variant="amber" className="w-full" onClick={goToSignIn}>
          Sign in
        </TapeDeckButton>
      </div>
    );
  } else {
    const { info } = state;
    const ok = checkPassword(password, confirm, info.username).allOk;
    body = (
      <form onSubmit={handleSubmit} className="space-y-5 text-left">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs font-mono">
          <dt className="text-[var(--text-muted)] uppercase">Username</dt>
          <dd className="text-[var(--text-primary)] break-all">{info.username}</dd>
          <dt className="text-[var(--text-muted)] uppercase">Purpose</dt>
          <dd className="text-[var(--text-primary)]">
            {info.purpose === 'reset' ? 'Password reset' : 'New account invitation'}
          </dd>
          <dt className="text-[var(--text-muted)] uppercase">Expires</dt>
          <dd className="text-[var(--text-primary)]">{formatExpiry(info.expires_at)}</dd>
        </dl>

        <PasswordSetupFields
          idPrefix="invite"
          username={info.username}
          password={password}
          confirm={confirm}
          onPasswordChange={setPassword}
          onConfirmChange={setConfirm}
          disabled={isSubmitting}
        />

        {submitError && <StatusMessage variant="error">{submitError}</StatusMessage>}

        <TapeDeckButton
          type="submit"
          size="lg"
          variant="amber"
          className="w-full"
          disabled={!ok || isSubmitting}
          icon={
            isSubmitting ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <KeyRound className="h-4 w-4" />
            )
          }
        >
          {info.purpose === 'reset' ? 'Set new password' : 'Set password'}
        </TapeDeckButton>
      </form>
    );
  }

  return (
    <div className="min-h-screen w-full flex items-center justify-center p-4 bg-[var(--bg-canvas)] text-[var(--text-primary)]">
      <MachinedCard className="max-w-md w-full p-6 sm:p-8 space-y-6 bg-[var(--bg-surface)]">
        <div className="text-center space-y-1">
          <h1 className="text-2xl font-black tracking-tight uppercase font-mono">
            Track<span className="text-[var(--accent-amber)]">Seerr</span>
          </h1>
          <p className="text-xs font-mono text-[var(--text-secondary)]">Set your password</p>
        </div>
        {body}
      </MachinedCard>
    </div>
  );
};
