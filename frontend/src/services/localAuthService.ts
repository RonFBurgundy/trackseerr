import { ApiError, apiRequest, setAuthToken } from './apiClient';
import type {
  InviteInfo,
  LocalLoginOutcome,
  LocalLoginPayload,
  LocalLoginResponse,
} from '@/types/account';

// Exact 401 details from plex_playlist_sync/local_login.py.
const DETAIL_MFA_REQUIRED = 'mfa_required';
const DETAIL_INVALID_CODE = 'Invalid authentication code';

export const INVALID_LINK_MESSAGE = 'This link is invalid or has expired';

/**
 * Local (username + password) sign-in. Failures are mapped to deliberately generic
 * messages so the UI never reveals whether an account exists or is locked for a
 * reason beyond what the server already tells every caller.
 */
export async function localLogin(payload: LocalLoginPayload): Promise<LocalLoginOutcome> {
  try {
    const res = await apiRequest<LocalLoginResponse | null>('/api/auth/local/login', {
      method: 'POST',
      body: payload,
      passthroughUnauthorized: true,
    });
    if (res?.token) {
      setAuthToken(res.token);
    }
    return { kind: 'success', mfaEnrollmentRequired: Boolean(res?.mfa_enrollment_required) };
  } catch (err: unknown) {
    if (err instanceof ApiError) {
      if (err.status === 401 && err.message === DETAIL_MFA_REQUIRED) {
        return { kind: 'mfa_required' };
      }
      if (err.status === 401 && err.message === DETAIL_INVALID_CODE) {
        return { kind: 'error', message: 'Invalid code' };
      }
      if (err.status === 401) {
        return { kind: 'error', message: 'Invalid username or password' };
      }
      if (err.status === 423) {
        return { kind: 'error', message: 'Account temporarily locked. Try again later.' };
      }
      if (err.status === 429) {
        return { kind: 'error', message: 'Too many sign-in attempts. Try again later.' };
      }
      return { kind: 'error', message: 'Sign-in failed. Please try again.' };
    }
    return { kind: 'error', message: 'Could not reach the server. Please try again.' };
  }
}

export type InviteLookup =
  | { kind: 'ok'; info: InviteInfo }
  | { kind: 'invalid' }
  | { kind: 'error'; message: string };

export async function getInvite(token: string): Promise<InviteLookup> {
  try {
    const info = await apiRequest<InviteInfo>(
      `/api/auth/invite/${encodeURIComponent(token)}`,
      { passthroughUnauthorized: true }
    );
    return { kind: 'ok', info };
  } catch (err: unknown) {
    if (err instanceof ApiError && err.status === 404) {
      return { kind: 'invalid' };
    }
    if (err instanceof ApiError && err.status === 429) {
      return { kind: 'error', message: 'Too many attempts. Try again later.' };
    }
    return { kind: 'error', message: 'Could not load this link. Please try again.' };
  }
}

export type InviteRedeem =
  | { kind: 'ok' }
  | { kind: 'invalid' }
  | { kind: 'error'; message: string };

export async function redeemInvite(token: string, password: string): Promise<InviteRedeem> {
  try {
    await apiRequest<unknown>(`/api/auth/invite/${encodeURIComponent(token)}`, {
      method: 'POST',
      body: { password },
      passthroughUnauthorized: true,
    });
    return { kind: 'ok' };
  } catch (err: unknown) {
    if (err instanceof ApiError) {
      if (err.status === 404) return { kind: 'invalid' };
      if (err.status === 429) {
        return { kind: 'error', message: 'Too many attempts. Try again later.' };
      }
      // 400 carries the server-side password policy message.
      return { kind: 'error', message: err.message };
    }
    return { kind: 'error', message: 'Could not reach the server. Please try again.' };
  }
}
