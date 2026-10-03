import { apiRequest } from './apiClient';
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

/**
 * Wire shape of one admin user row. The contract says `quotas (effective + overrides)`
 * without pinning the layout, so the normaliser accepts either a nested
 * `quotas: {effective, overrides}` or a flat effective `quotas` plus `quota_*` override columns.
 */
interface RawQuotaSet {
  tracks?: Nullable<number>;
  albums?: Nullable<number>;
  discographies?: Nullable<number>;
  window_days?: Nullable<number>;
}

interface RawAdminUser {
  id: string | number;
  username?: Nullable<string>;
  plex_username?: Nullable<string>;
  email?: Nullable<string>;
  auth_type?: AuthType;
  is_admin?: boolean;
  permissions?: Nullable<number>;
  disabled?: boolean;
  mfa_enabled?: boolean;
  last_login_at?: Nullable<string>;
  created_at?: Nullable<string>;
  quotas?: Nullable<RawQuotaSet & { effective?: RawQuotaSet; overrides?: RawQuotaSet }>;
  overrides?: Nullable<RawQuotaSet>;
  quota_tracks?: Nullable<number>;
  quota_albums?: Nullable<number>;
  quota_discographies?: Nullable<number>;
  quota_window_days?: Nullable<number>;
  usage?: Nullable<Partial<QuotaUsage>>;
}

function numOrNull(v: Nullable<number>): number | null {
  return typeof v === 'number' ? v : null;
}

export function normalizeAdminUser(raw: RawAdminUser): AdminUser {
  const q = raw.quotas ?? null;
  const effectiveSrc: RawQuotaSet | null = q ? (q.effective ?? q) : null;
  const overrideSrc: RawQuotaSet | null = raw.overrides ?? q?.overrides ?? null;

  const overrides: QuotaOverrides = {
    tracks: numOrNull(overrideSrc ? overrideSrc.tracks : raw.quota_tracks),
    albums: numOrNull(overrideSrc ? overrideSrc.albums : raw.quota_albums),
    discographies: numOrNull(overrideSrc ? overrideSrc.discographies : raw.quota_discographies),
    window_days: numOrNull(overrideSrc ? overrideSrc.window_days : raw.quota_window_days),
  };

  const quotas: EffectiveQuotas | null = effectiveSrc
    ? {
        tracks: effectiveSrc.tracks ?? 0,
        albums: effectiveSrc.albums ?? 0,
        discographies: effectiveSrc.discographies ?? 0,
        window_days: effectiveSrc.window_days ?? 0,
      }
    : null;

  const usage: QuotaUsage | null = raw.usage
    ? {
        tracks: raw.usage.tracks ?? 0,
        albums: raw.usage.albums ?? 0,
        discographies: raw.usage.discographies ?? 0,
      }
    : null;

  return {
    id: String(raw.id),
    username: raw.username ?? raw.plex_username ?? String(raw.id),
    email: raw.email ?? null,
    auth_type: raw.auth_type ?? 'plex',
    is_admin: Boolean(raw.is_admin),
    permissions: raw.permissions ?? 0,
    disabled: Boolean(raw.disabled),
    mfa_enabled: Boolean(raw.mfa_enabled),
    last_login_at: raw.last_login_at ?? null,
    created_at: raw.created_at ?? null,
    quotas,
    overrides,
    usage,
  };
}

const base = (id: string): string => `/api/admin/users/${encodeURIComponent(id)}`;

export async function listAdminUsers(): Promise<AdminUser[]> {
  const rows = await apiRequest<RawAdminUser[]>('/api/admin/users');
  return (rows ?? []).map(normalizeAdminUser);
}

export async function createLocalUser(
  payload: CreateLocalUserPayload
): Promise<CreateLocalUserResult> {
  const res = await apiRequest<{ user: RawAdminUser; invite_url: string }>('/api/admin/users', {
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

export async function resetUserPassword(id: string): Promise<{ reset_url: string }> {
  return apiRequest<{ reset_url: string }>(`${base(id)}/reset-password`, { method: 'POST' });
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
