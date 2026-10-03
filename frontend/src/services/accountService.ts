import { apiRequest } from './apiClient';
import type {
  AccountInfo,
  ChangePasswordPayload,
  MfaConfirmResponse,
  MfaCredentialPayload,
  MfaSetupResponse,
} from '@/types/account';

export async function getAccount(): Promise<AccountInfo> {
  return apiRequest<AccountInfo>('/api/account');
}

// Wrong credentials on these calls return 400 (shown inline); a 401 means the session was
// revoked, so it takes the normal global sign-out path.
export async function changePassword(payload: ChangePasswordPayload): Promise<void> {
  await apiRequest<unknown>('/api/account/password', {
    method: 'POST',
    body: payload,
  });
}

export async function startMfaSetup(password: string): Promise<MfaSetupResponse> {
  return apiRequest<MfaSetupResponse>('/api/account/mfa/setup', {
    method: 'POST',
    body: { password },
  });
}

export async function confirmMfa(code: string): Promise<MfaConfirmResponse> {
  return apiRequest<MfaConfirmResponse>('/api/account/mfa/confirm', {
    method: 'POST',
    body: { code },
  });
}

export async function disableMfa(payload: MfaCredentialPayload): Promise<void> {
  await apiRequest<unknown>('/api/account/mfa/disable', {
    method: 'POST',
    body: payload,
  });
}

export async function regenerateRecoveryCodes(
  payload: MfaCredentialPayload
): Promise<MfaConfirmResponse> {
  return apiRequest<MfaConfirmResponse>('/api/account/mfa/recovery-codes', {
    method: 'POST',
    body: payload,
  });
}
