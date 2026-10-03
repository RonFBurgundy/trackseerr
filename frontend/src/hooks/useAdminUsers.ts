import { useState, useEffect, useCallback } from 'react';
import type {
  AccountDefaultSettings,
  AdminUser,
  CreateLocalUserPayload,
  CreateLocalUserResult,
  UpdateUserPayload,
} from '@/types/account';
import {
  listAdminUsers,
  createLocalUser,
  updateAdminUser,
  setUserDisabled,
  resetUserPassword,
  resetUserMfa,
  revokeUserSessions,
  deleteAdminUser,
  getAccountDefaults,
  updateAccountDefaults,
} from '@/services/adminUsersService';
import { errorMessage } from '@/services/apiClient';

export interface UseAdminUsersReturn {
  users: AdminUser[];
  defaults: AccountDefaultSettings | null;
  isLoading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  /** Mutations throw on failure; callers render `errorMessage(err, ...)` inline. */
  create: (payload: CreateLocalUserPayload) => Promise<CreateLocalUserResult>;
  update: (id: string, payload: UpdateUserPayload) => Promise<void>;
  setDisabled: (id: string, disabled: boolean) => Promise<void>;
  resetPassword: (id: string) => Promise<string>;
  resetMfa: (id: string) => Promise<void>;
  revokeSessions: (id: string) => Promise<void>;
  remove: (id: string, confirmUsername: string) => Promise<void>;
  saveDefaults: (next: AccountDefaultSettings) => Promise<void>;
}

export function useAdminUsers(enabled: boolean): UseAdminUsersReturn {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [defaults, setDefaults] = useState<AccountDefaultSettings | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(enabled);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const [rows, defs] = await Promise.all([listAdminUsers(), getAccountDefaults()]);
      setUsers(rows);
      setDefaults(defs);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to load users'));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) {
      void refresh();
    }
  }, [enabled, refresh]);

  const create = useCallback(
    async (payload: CreateLocalUserPayload) => {
      const res = await createLocalUser(payload);
      await refresh();
      return res;
    },
    [refresh]
  );

  const update = useCallback(
    async (id: string, payload: UpdateUserPayload) => {
      await updateAdminUser(id, payload);
      await refresh();
    },
    [refresh]
  );

  const setDisabled = useCallback(
    async (id: string, disabled: boolean) => {
      await setUserDisabled(id, disabled);
      await refresh();
    },
    [refresh]
  );

  const resetPassword = useCallback(async (id: string) => {
    const res = await resetUserPassword(id);
    return res.reset_url;
  }, []);

  const resetMfa = useCallback(
    async (id: string) => {
      await resetUserMfa(id);
      await refresh();
    },
    [refresh]
  );

  const revokeSessions = useCallback(async (id: string) => {
    await revokeUserSessions(id);
  }, []);

  const remove = useCallback(
    async (id: string, confirmUsername: string) => {
      await deleteAdminUser(id, confirmUsername);
      await refresh();
    },
    [refresh]
  );

  const saveDefaults = useCallback(async (next: AccountDefaultSettings) => {
    setDefaults(await updateAccountDefaults(next));
  }, []);

  return {
    users,
    defaults,
    isLoading,
    error,
    refresh,
    create,
    update,
    setDisabled,
    resetPassword,
    resetMfa,
    revokeSessions,
    remove,
    saveDefaults,
  };
}
