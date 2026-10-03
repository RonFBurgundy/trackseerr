import { useState, useEffect, useCallback } from 'react';
import type { AccountInfo, MfaSetupResponse } from '@/types/account';
import {
  getAccount,
  changePassword as apiChangePassword,
  startMfaSetup,
  confirmMfa,
  disableMfa as apiDisableMfa,
  regenerateRecoveryCodes as apiRegenerateCodes,
} from '@/services/accountService';
import { errorMessage } from '@/services/apiClient';

export interface UseAccountReturn {
  account: AccountInfo | null;
  isLoading: boolean;
  error: string | null;
  /** Pending MFA enrollment (secret + otpauth URI), until confirmed or cancelled. */
  mfaSetup: MfaSetupResponse | null;
  /** Freshly issued recovery codes; shown once, cleared by dismissRecoveryCodes. */
  recoveryCodes: string[] | null;
  refresh: () => Promise<void>;
  changePassword: (current: string, next: string) => Promise<string | null>;
  beginMfaSetup: (password: string) => Promise<string | null>;
  cancelMfaSetup: () => void;
  confirmMfaSetup: (code: string) => Promise<string | null>;
  disableMfa: (password: string, code: string) => Promise<string | null>;
  regenerateRecoveryCodes: (password: string, code: string) => Promise<string | null>;
  dismissRecoveryCodes: () => void;
}

/**
 * Account self-service state. Mutating actions resolve to an error message
 * (or null on success) so forms can show it inline.
 */
export function useAccount(enabled: boolean = true): UseAccountReturn {
  const [account, setAccount] = useState<AccountInfo | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(enabled);
  const [error, setError] = useState<string | null>(null);
  const [mfaSetup, setMfaSetup] = useState<MfaSetupResponse | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      setAccount(await getAccount());
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to load account'));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) {
      void refresh();
    }
  }, [enabled, refresh]);

  const changePassword = useCallback(async (current: string, next: string) => {
    try {
      await apiChangePassword({ current_password: current, new_password: next });
      return null;
    } catch (err: unknown) {
      return errorMessage(err, 'Failed to change password');
    }
  }, []);

  const beginMfaSetup = useCallback(async (password: string) => {
    try {
      setMfaSetup(await startMfaSetup(password));
      return null;
    } catch (err: unknown) {
      return errorMessage(err, 'Failed to start MFA setup');
    }
  }, []);

  const cancelMfaSetup = useCallback(() => setMfaSetup(null), []);

  const confirmMfaSetup = useCallback(
    async (code: string) => {
      try {
        const res = await confirmMfa(code.trim());
        setMfaSetup(null);
        setRecoveryCodes(res.recovery_codes);
        await refresh();
        return null;
      } catch (err: unknown) {
        return errorMessage(err, 'That code was not accepted');
      }
    },
    [refresh]
  );

  const disableMfa = useCallback(
    async (password: string, code: string) => {
      try {
        await apiDisableMfa({ password, code: code.trim() });
        await refresh();
        return null;
      } catch (err: unknown) {
        return errorMessage(err, 'Failed to disable MFA');
      }
    },
    [refresh]
  );

  const regenerateRecoveryCodes = useCallback(
    async (password: string, code: string) => {
      try {
        const res = await apiRegenerateCodes({ password, code: code.trim() });
        setRecoveryCodes(res.recovery_codes);
        await refresh();
        return null;
      } catch (err: unknown) {
        return errorMessage(err, 'Failed to regenerate recovery codes');
      }
    },
    [refresh]
  );

  const dismissRecoveryCodes = useCallback(() => setRecoveryCodes(null), []);

  return {
    account,
    isLoading,
    error,
    mfaSetup,
    recoveryCodes,
    refresh,
    changePassword,
    beginMfaSetup,
    cancelMfaSetup,
    confirmMfaSetup,
    disableMfa,
    regenerateRecoveryCodes,
    dismissRecoveryCodes,
  };
}
