import type { Narrow, Schema } from './apiSchema';
/**
 * Types for local accounts, MFA, request quotas and admin user management.
 * Mirrors docs/users-and-accounts.md.
 */

export type AuthType = 'plex' | 'local' | 'jellyfin';
export type QuotaKind = 'tracks' | 'albums' | 'discographies';

export const QUOTA_KINDS: readonly QuotaKind[] = ['tracks', 'albums', 'discographies'] as const;

export const QUOTA_LABELS: Record<QuotaKind, string> = {
  tracks: 'Tracks',
  albums: 'Albums',
  discographies: 'Discographies',
};

export type QuotaUsage = Schema<'QuotaUsage'>;

export type AccountQuotas = Schema<'QuotaSnapshot'>;

export type AccountInfo = Narrow<Schema<'AccountResponse'>, { auth_type: AuthType }>;

export type MfaSetupResponse = Schema<'MfaSetupResponse'>;

export type MfaConfirmResponse = Schema<'RecoveryCodesResponse'>;

export interface ChangePasswordPayload {
  current_password: string;
  new_password: string;
}

export interface MfaCredentialPayload {
  password: string;
  code: string;
}

// ---- Local login -----------------------------------------------------------

export interface LocalLoginPayload {
  username: string;
  password: string;
  totp_code?: string;
  recovery_code?: string;
}

export type LocalLoginResponse = Schema<'LocalLoginResponse'>;

export type LocalLoginOutcome =
  | { kind: 'success'; mfaEnrollmentRequired: boolean }
  | { kind: 'mfa_required' }
  | { kind: 'error'; message: string };

// ---- Invite / reset --------------------------------------------------------

export type InvitePurpose = 'invite' | 'reset';

export type InviteInfo = Narrow<Schema<'InviteInfoResponse'>, { purpose: InvitePurpose }>;

// ---- Admin user management -------------------------------------------------

export const PERMISSION_FLAGS = [
  { bit: 1, key: 'ADMIN', label: 'Admin' },
  { bit: 2, key: 'REQUEST', label: 'Request music' },
  { bit: 4, key: 'AUTO_APPROVE', label: 'Auto-approve tracks' },
  { bit: 8, key: 'AUTO_APPROVE_ALBUM', label: 'Auto-approve albums' },
  { bit: 16, key: 'MANAGE_REQUESTS', label: 'Manage requests' },
  { bit: 32, key: 'REPORT_ISSUE', label: 'Report issues' },
  { bit: 64, key: 'AUTO_APPROVE_DISCOGRAPHY', label: 'Auto-approve discographies' },
  { bit: 128, key: 'AUTO_REQUEST_PLAYLISTS', label: 'Auto-request playlist tracks' },
] as const;

export const PERMISSION_ADMIN = 1;
export const PERMISSION_ALL_KNOWN_BITS = PERMISSION_FLAGS.reduce((acc, f) => acc | f.bit, 0);

export interface PermissionPreset {
  id: string;
  label: string;
  permissions: number;
}

export const PERMISSION_PRESETS: readonly PermissionPreset[] = [
  { id: 'requester', label: 'Requester', permissions: 2 | 32 },
  { id: 'trusted', label: 'Trusted (auto-approve tracks + albums)', permissions: 2 | 4 | 8 | 32 },
  { id: 'moderator', label: 'Moderator', permissions: 2 | 4 | 8 | 16 | 32 | 64 },
] as const;

/** A quota override per type; null means "use the global default". */
export interface QuotaOverrides {
  tracks: number | null;
  albums: number | null;
  discographies: number | null;
  window_days: number | null;
}

export interface EffectiveQuotas {
  tracks: number;
  albums: number;
  discographies: number;
  window_days: number;
}

export interface AdminUser {
  id: string;
  username: string;
  email: string | null;
  auth_type: AuthType;
  is_admin: boolean;
  permissions: number;
  disabled: boolean;
  mfa_enabled: boolean;
  last_login_at: string | null;
  created_at: string | null;
  quotas: EffectiveQuotas | null;
  overrides: QuotaOverrides;
  usage: QuotaUsage | null;
}

export interface CreateLocalUserPayload {
  username: string;
  email?: string;
  permissions?: number;
  quotas?: Partial<{
    tracks: number | null;
    albums: number | null;
    discographies: number | null;
    window_days: number | null;
  }>;
}

export interface CreateLocalUserResult {
  user: AdminUser;
  invite_url: string;
}

export interface UpdateUserPayload {
  permissions?: number;
  email?: string | null;
  quota_tracks?: number | null;
  quota_albums?: number | null;
  quota_discographies?: number | null;
  quota_window_days?: number | null;
}

export type AccountDefaultSettings = Schema<'AccountSettings'>;

// ---- Batch requests --------------------------------------------------------

export interface BatchRequestItem {
  item_type: 'album' | 'track';
  title: string;
  artist: string;
  album?: string;
  cover_url?: string;
  release_date?: string;
  foreign_id?: string;
}

export interface DiscographyBatchPayload {
  kind: 'discography';
  artist: string;
  requests: BatchRequestItem[];
}
