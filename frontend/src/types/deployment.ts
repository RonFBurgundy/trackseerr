import type { DeploymentTier } from './models';

/** GET /api/admin/gateway-status */
export type GatewayState = 'online' | 'stale' | 'never_seen' | 'not_used';

export interface GatewayStatus {
  configured: boolean;
  last_seen_at: string | null;
  version: string | null;
  protocol: number | null;
  version_match: boolean | null;
  active_sessions: number | null;
  state: GatewayState;
  public_url: string | null;
}

/** GET /api/admin/role-change-notice and POST .../dismiss (both return this shape). */
export interface RoleChangeNotice {
  active: boolean;
  from_role: string | null;
  to_role: string | null;
  changed_at: string | null;
  checklist: string[];
}

/** GET /api/health: `tier` is optional because older servers return only `status`. */
export interface HealthResponse {
  status: string;
  tier?: DeploymentTier;
}
