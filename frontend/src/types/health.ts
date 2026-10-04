/** Shape of GET /api/health. While booting, `status` is "starting" and `step` names the current boot step. */
export interface HealthStatus {
  status: 'ok' | 'starting';
  tier?: string;
  step?: string;
}
