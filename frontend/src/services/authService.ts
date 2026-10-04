import { apiRequest, setAuthToken } from './apiClient';
import type { AuthPinResponse, AuthVerifyResponse, DeploymentTier, User } from '@/types/models';

export async function startPlexAuth(forwardUrl?: string): Promise<AuthPinResponse> {
  return apiRequest<AuthPinResponse>('/api/auth/plex/pin', {
    method: 'POST',
    ...(forwardUrl ? { body: { forward_url: forwardUrl } } : {}),
  });
}

export async function verifyPin(pinId: number): Promise<AuthVerifyResponse> {
  const result = await apiRequest<AuthVerifyResponse>('/api/auth/plex/verify', {
    method: 'POST',
    body: { pin_id: pinId },
  });
  if (result.token) {
    setAuthToken(result.token);
  }
  return result;
}

export async function pollPin(
  pinId: number,
  signal?: AbortSignal
): Promise<AuthVerifyResponse> {
  return apiRequest<AuthVerifyResponse>('/api/auth/plex/verify', {
    method: 'POST',
    body: { pin_id: pinId },
    signal,
  });
}

export async function getCurrentUser(): Promise<User | null> {
  try {
    const res = await apiRequest<{ user: User; tier?: DeploymentTier }>('/api/auth/me');
    if (!res?.user) return null;
    const tier = res.user.tier ?? res.tier;
    return tier ? { ...res.user, tier } : res.user;
  } catch {
    return null;
  }
}

export async function logout(): Promise<void> {
  try {
    await apiRequest<void>('/api/auth/logout', { method: 'POST' });
  } finally {
    setAuthToken(null);
  }
}
