import type { HealthStatus } from '@/types/health';

const HEALTH_TIMEOUT_MS = 4000;

function isHealthStatus(value: unknown): value is HealthStatus {
  if (typeof value !== 'object' || value === null) return false;
  const status = (value as { status?: unknown }).status;
  return status === 'ok' || status === 'starting';
}

/**
 * Reads the unauthenticated health endpoint. Plain fetch on purpose: a 401/503 here must never
 * trigger the global session-drop handling in apiRequest. Returns null when the server is
 * unreachable or the body is not a health payload.
 */
export async function fetchHealth(): Promise<HealthStatus | null> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS);
  try {
    const response = await fetch('/api/health', {
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      signal: controller.signal,
    });
    const body: unknown = await response.json();
    return isHealthStatus(body) ? body : null;
  } catch (err: unknown) {
    if (err instanceof TypeError || err instanceof SyntaxError) return null;
    if (err instanceof DOMException && err.name === 'AbortError') return null;
    throw err;
  } finally {
    window.clearTimeout(timer);
  }
}
