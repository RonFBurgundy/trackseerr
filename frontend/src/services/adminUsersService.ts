import { apiRequest } from './apiClient';
import type { Schema } from '@/types/apiSchema';
import type {
  AccountDefaultSettings,
  AdminUser,
  AuthType,
  CreateLocalUserPayload,
  CreateLocalUserResult,
  EffectiveQuotas,
  QuotaOverrides,
  QuotaUsage,
  UpdateUserPayload,
} from '@/types/account';

type Nullable<T> = T | null | undefined;

function numOrNull(v: Nullable<number>): number | null {
  return typeof v === 'number' ? v : null;
}

function toAuthType(value: string): AuthType {
  return value === 'local' || value === 'jellyfin' ? value : 'plex';
}

export function normalizeAdminUser(raw: Schema<'AdminUser'>): AdminUser {
  const { effective, overrides: overrideSrc } = raw.quotas;

  const overrides: QuotaOverrides = {
    tracks: numOrNull(overrideSrc.tracks),
    albums: numOrNull(overrideSrc.albums),
    discographies: numOrNull(overrideSrc.discographies),
    window_days: numOrNull(overrideSrc.window_days),
  };

  const quotas: EffectiveQuotas = {
    tracks: effective.tracks ?? 0,
    albums: effective.albums ?? 0,
    discographies: effective.discographies ?? 0,
    window_days: effective.window_days ?? 0,
  };

  const usage: QuotaUsage = {
    tracks: raw.usage.tracks,
    albums: raw.usage.albums,
    discographies: raw.usage.discographies,
  };

  return {
    id: raw.id,
    username: raw.username,
    email: raw.email ?? null,
    auth_type: toAuthType(raw.auth_type),
    is_admin: raw.is_admin,
    permissions: raw.permissions,
    disabled: raw.disabled,
    mfa_enabled: raw.mfa_enabled,
    last_login_at: raw.last_login_at ?? null,
    created_at: raw.created_at ?? null,
    quotas,
    overrides,
    usage,
  };
}

const base = (id: string): string => `/api/admin/users/${encodeURIComponent(id)}`;

export async function listAdminUsers(): Promise<AdminUser[]> {
  const rows = await apiRequest<Schema<'AdminUser'>[]>('/api/admin/users');
  return (rows ?? []).map(normalizeAdminUser);
}

export async function createLocalUser(
  payload: CreateLocalUserPayload
): Promise<CreateLocalUserResult> {
  const res = await apiRequest<Schema<'CreatedAdminUser'>>('/api/admin/users', {
    method: 'POST',
    body: payload,
  });
  return { user: normalizeAdminUser(res.user), invite_url: res.invite_url };
}

export async function updateAdminUser(id: string, payload: UpdateUserPayload): Promise<void> {
  await apiRequest<unknown>(base(id), { method: 'PATCH', body: payload });
}

export async function setUserDisabled(id: string, disabled: boolean): Promise<void> {
  await apiRequest<unknown>(`${base(id)}/${disabled ? 'disable' : 'enable'}`, { method: 'POST' });
}

export async function resetUserPassword(id: string): Promise<Schema<'ResetPasswordResponse'>> {
  return apiRequest<Schema<'ResetPasswordResponse'>>(`${base(id)}/reset-password`, { method: 'POST' });
}

export async function resetUserMfa(id: string): Promise<void> {
  await apiRequest<unknown>(`${base(id)}/reset-mfa`, { method: 'POST' });
}

export async function revokeUserSessions(id: string): Promise<void> {
  await apiRequest<unknown>(`${base(id)}/revoke-sessions`, { method: 'POST' });
}

export async function deleteAdminUser(id: string, confirmUsername: string): Promise<void> {
  await apiRequest<unknown>(base(id), {
    method: 'DELETE',
    body: { confirm_username: confirmUsername },
  });
}

export async function restoreAdminUser(id: string): Promise<void> {
  await apiRequest<unknown>(`${base(id)}/restore`, { method: 'POST' });
}

export async function getAccountDefaults(): Promise<AccountDefaultSettings> {
  return apiRequest<AccountDefaultSettings>('/api/admin/settings/accounts');
}

export async function updateAccountDefaults(
  payload: AccountDefaultSettings
): Promise<AccountDefaultSettings> {
  return apiRequest<AccountDefaultSettings>('/api/admin/settings/accounts', {
    method: 'PUT',
    body: payload,
  });
}
