import { apiRequest } from './apiClient';
import type {
  DeploymentTier,
  GatewayStatus,
  HealthResponse,
  RoleChangeNotice,
} from '@/types';

const TIERS: readonly DeploymentTier[] = ['gateway', 'core', 'all-in-one'];

/** Unauthenticated health probe; resolves the tier when the server exposes it, else null. */
export async function getPublicTier(): Promise<DeploymentTier | null> {
  try {
    const res = await apiRequest<HealthResponse>('/api/health', {
      passthroughUnauthorized: true,
    });
    return res?.tier && TIERS.includes(res.tier) ? res.tier : null;
  } catch {
    // Identity is cosmetic: fall back to the default branding if health is unreachable.
    return null;
  }
}

export async function getGatewayStatus(): Promise<GatewayStatus> {
  return await apiRequest<GatewayStatus>('/api/admin/gateway-status');
}

export async function getRoleChangeNotice(): Promise<RoleChangeNotice> {
  return await apiRequest<RoleChangeNotice>('/api/admin/role-change-notice');
}

export async function dismissRoleChangeNotice(): Promise<RoleChangeNotice> {
  return await apiRequest<RoleChangeNotice>('/api/admin/role-change-notice/dismiss', { method: 'POST' });
}
