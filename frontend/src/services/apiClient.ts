/**
 * Central API Client for TrackSeerr React SPA.
 * Handles authentication tokens, JSON serialization, and error dispatching.
 */

const TOKEN_KEY = 'trackseerr_auth_token';

export function getAuthToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setAuthToken(token: string | null): void {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

export interface ApiRequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown;
  /**
   * When true, a 401 is surfaced as an ApiError (status 401 + server detail) instead of
   * firing the global "unauthorized" session-drop event. Used by login, invite and
   * credential-confirming account calls, where 401 is an expected, displayable outcome.
   */
  passthroughUnauthorized?: boolean;
}

/** Error carrying the HTTP status so callers can map specific failures. */
export class ApiError extends Error {
  readonly status: number;
  /** The server's raw `detail` (string, list of strings/validation items, or an object), for callers that need structure. */
  readonly detail: unknown;
  /** The full parsed JSON body, for responses that carry fields beside `detail` (e.g. `existing_issue_id`). */
  readonly body: unknown;

  constructor(message: string, status: number, detail?: unknown, body?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Flattens FastAPI's `detail` (string, validation items, plain strings, or `{message}`) into one line. */
function detailMessage(detail: unknown): string | null {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const parts = detail.flatMap((item): string[] => {
      if (typeof item === 'string') return [item];
      if (isRecord(item) && typeof item.msg === 'string') return [item.msg];
      return [];
    });
    return parts.length > 0 ? parts.join('; ') : null;
  }
  if (isRecord(detail) && typeof detail.message === 'string') return detail.message;
  return null;
}

export async function apiRequest<T>(
  endpoint: string,
  rawOptions: ApiRequestOptions = {}
): Promise<T> {
  const { passthroughUnauthorized = false, ...options } = rawOptions;
  const headers: Record<string, string> = {
    Accept: 'application/json',
  };

  const token = getAuthToken();
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  let bodyContent: BodyInit | null | undefined = undefined;
  if (options.body !== undefined && options.body !== null) {
    if (typeof options.body === 'string') {
      bodyContent = options.body;
      if (options.body.startsWith('{') || options.body.startsWith('[')) {
        headers['Content-Type'] = 'application/json';
      }
    } else if (options.body instanceof FormData) {
      bodyContent = options.body;
    } else {
      headers['Content-Type'] = 'application/json';
      bodyContent = JSON.stringify(options.body);
    }
  }

  // Merge extra headers from options
  if (options.headers) {
    if (options.headers instanceof Headers) {
      options.headers.forEach((val, key) => {
        headers[key] = val;
      });
    } else if (Array.isArray(options.headers)) {
      for (const [key, val] of options.headers) {
        headers[key] = val;
      }
    } else {
      Object.assign(headers, options.headers);
    }
  }

  const response = await fetch(endpoint, {
    ...options,
    headers,
    body: bodyContent,
    credentials: 'same-origin',
  });

  if (response.status === 401 && !passthroughUnauthorized) {
    // A request that left before the token changed (typically fired pre-login, answered after the new
    // session was stored) describes the OLD credentials. It must not drop the fresh session.
    const credentialsChanged = getAuthToken() !== token;
    if (!credentialsChanged && typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('trackseerr:unauthorized'));
    }
    throw new Error('Unauthorized');
  }

  if (response.status === 204) {
    return null as T;
  }

  const data: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    const detail: unknown = isRecord(data) ? data.detail : undefined;
    const detailMsg = detailMessage(detail) ?? `HTTP Error ${response.status}: ${response.statusText}`;
    throw new ApiError(detailMsg, response.status, detail, data);
  }

  return data as T;
}

/** Extracts a displayable message from an unknown thrown value. */
export function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}
