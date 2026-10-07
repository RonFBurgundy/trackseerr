import React from 'react';
import { MachinedCard, StatusMessage, CassetteLoader } from '@/components/ui';
import type { UseAccountReturn } from '@/hooks/useAccount';
import { QuotaBars } from './QuotaBars';
import { ChangePasswordForm } from './ChangePasswordForm';
import { MfaPanel } from './MfaPanel';

export interface AccountPanelProps {
  accountHook: UseAccountReturn;
  isAdmin?: boolean;
  enrollmentBlocking?: boolean;
}

export const AccountPanel: React.FC<AccountPanelProps> = ({
  accountHook,
  isAdmin = false,
  enrollmentBlocking = false,
}) => {
  const { account, isLoading, error } = accountHook;

  if (isLoading && !account) {
    return (
      <div className="flex justify-center py-16">
        <CassetteLoader size="md" />
      </div>
    );
  }
  if (!account) {
    return <StatusMessage variant="error">{error ?? 'Account unavailable'}</StatusMessage>;
  }

  const isLocal = account.auth_type === 'local';

  return (
    <div className="space-y-4 max-w-2xl">
      <MachinedCard className="p-3 sm:p-6 space-y-1">
        <h3 className="text-sm font-bold uppercase tracking-wider">Account</h3>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs font-mono pt-2">
          <dt className="text-[var(--text-muted)] uppercase">Username</dt>
          <dd className="break-all">{account.username}</dd>
          <dt className="text-[var(--text-muted)] uppercase">Sign-in</dt>
          <dd>{isLocal ? 'Local account' : 'Plex'}</dd>
        </dl>
      </MachinedCard>

      <MachinedCard className="p-3 sm:p-6 space-y-4">
        <h3 className="text-sm font-bold uppercase tracking-wider">Request quota</h3>
        <QuotaBars account={account} isAdmin={isAdmin} />
      </MachinedCard>

      {isLocal ? (
        <>
          <ChangePasswordForm username={account.username} onChange={accountHook.changePassword} />
          <MfaPanel accountHook={accountHook} enrollmentBlocking={enrollmentBlocking} />
        </>
      ) : (
        <MachinedCard className="p-3 sm:p-6">
          <p className="text-xs font-mono text-[var(--text-secondary)]">
            Your password and two-factor settings are managed by Plex.
          </p>
        </MachinedCard>
      )}
    </div>
  );
};
