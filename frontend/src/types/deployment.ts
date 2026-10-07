import type { Narrow, Schema } from './apiSchema';
import type { DeploymentTier } from './models';

/** GET /api/admin/gateway-status */
export type GatewayState = 'online' | 'stale' | 'never_seen' | 'not_used';

export type GatewayStatus = Narrow<Schema<'GatewayStatus'>, { state: GatewayState }>;

export type RoleChangeNotice = Schema<'RoleChangeNotice'>;

/** GET /api/health: `tier` is optional because older servers return only `status`. */
export interface HealthResponse {
  status: string;
  tier?: DeploymentTier;
}
