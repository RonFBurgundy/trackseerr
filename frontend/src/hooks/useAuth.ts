import { useState, useEffect, useCallback, useRef } from 'react';
import type { DeploymentTier, User } from '@/types/models';
import {
  getCurrentUser,
  startPlexAuth,
  verifyPin,
  logout as apiLogout,
} from '@/services/authService';
import { getAuthToken, setAuthToken } from '@/services/apiClient';

export type PlexAuthPhase = 'idle' | 'starting' | 'popup' | 'blocked' | 'redirecting';

const PLEX_PIN_STORAGE_KEY = 'trackseerr:plexPin';
const PLEX_PIN_MAX_AGE_MS = 10 * 60 * 1000;
const POLL_INTERVAL_MS = 2000;
const POLL_MAX_ATTEMPTS = 60; // 2 minutes

function prefersRedirectFlow(): boolean {
  try {
    return (
      window.matchMedia('(pointer: coarse)').matches ||
      window.matchMedia('(max-width: 640px)').matches
    );
  } catch {
    return false;
  }
}

function storePendingPin(pinId: number): void {
  try {
    window.sessionStorage.setItem(
      PLEX_PIN_STORAGE_KEY,
      JSON.stringify({ pinId, at: Date.now() })
    );
  } catch {
    // Storage unavailable (private mode); redirect sign-in cannot resume, popup flow unaffected.
  }
}

function clearPendingPin(): void {
  try {
    window.sessionStorage.removeItem(PLEX_PIN_STORAGE_KEY);
  } catch {
    // ignore
  }
}

function readPendingPin(): number | null {
  try {
    const raw = window.sessionStorage.getItem(PLEX_PIN_STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed === 'object' &&
      parsed !== null &&
      typeof (parsed as { pinId?: unknown }).pinId === 'number' &&
      typeof (parsed as { at?: unknown }).at === 'number' &&
      Date.now() - (parsed as { at: number }).at < PLEX_PIN_MAX_AGE_MS
    ) {
      return (parsed as { pinId: number }).pinId;
    }
    clearPendingPin();
    return null;
  } catch {
    return null;
  }
}

export interface UseAuthReturn {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  isAdmin: boolean;
  tier: DeploymentTier;
  canUseAdminUi: boolean;
  isAuthenticating: boolean;
  authError: string | null;
  loginWithPlex: () => Promise<void>;
  /** Where the Plex sign-in currently stands (popup open / blocked / redirecting). */
  plexAuthPhase: PlexAuthPhase;
  /** Plex auth URL to offer as a manual link when the popup was blocked. */
  plexAuthUrl: string | null;
  cancelLogin: () => void;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
  /** True right after a local sign-in that must enroll in MFA before anything else works. */
  mfaEnrollmentRequired: boolean;
  /** Call after POST /api/auth/local/login succeeded: loads the session user. */
  completeLocalSignIn: (opts: { mfaEnrollmentRequired: boolean }) => Promise<void>;
}

export function useAuth(): UseAuthReturn {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(getAuthToken());
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isAuthenticating, setIsAuthenticating] = useState<boolean>(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [mfaEnrollmentRequired, setMfaEnrollmentRequired] = useState<boolean>(false);

  const pollTimerRef = useRef<number | null>(null);
  const popupRef = useRef<Window | null>(null);
  const [plexAuthPhase, setPlexAuthPhase] = useState<PlexAuthPhase>('idle');
  const [plexAuthUrl, setPlexAuthUrl] = useState<string | null>(null);

  const cancelLogin = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (popupRef.current && !popupRef.current.closed) {
      popupRef.current.close();
      popupRef.current = null;
    }
    clearPendingPin();
    setPlexAuthPhase('idle');
    setPlexAuthUrl(null);
    setIsAuthenticating(false);
  }, []);

  const pollPin = useCallback(
    (pinId: number, maxAttempts: number) => {
      let attempts = 0;
      if (pollTimerRef.current !== null) window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = window.setInterval(async () => {
        attempts++;
        if (attempts > maxAttempts || (popupRef.current && popupRef.current.closed)) {
          cancelLogin();
          return;
        }
        try {
          const verifyRes = await verifyPin(pinId);
          if (verifyRes && verifyRes.token && verifyRes.user) {
            cancelLogin();
            setUser(verifyRes.user);
            setToken(verifyRes.token);
          }
        } catch {
          // Still waiting for user authorization or transient network delay
        }
      }, POLL_INTERVAL_MS);
    },
    [cancelLogin]
  );

  const refreshUser = useCallback(async () => {
    try {
      const currentUser = await getCurrentUser();
      if (currentUser) {
        setUser(currentUser);
        setToken(getAuthToken());
      } else {
        setUser(null);
        setToken(null);
      }
    } catch {
      setUser(null);
      setToken(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshUser();

    const handleUnauthorized = () => {
      setUser(null);
      setToken(null);
      setAuthToken(null);
    };

    window.addEventListener('trackseerr:unauthorized', handleUnauthorized);
    return () => {
      window.removeEventListener('trackseerr:unauthorized', handleUnauthorized);
      cancelLogin();
    };
  }, [refreshUser, cancelLogin]);

  const loginWithPlex = useCallback(async () => {
    setIsAuthenticating(true);
    setAuthError(null);
    setPlexAuthUrl(null);

    // Mobile browsers handle popups poorly: navigate the page itself and resume on return.
    const redirectFlow = prefersRedirectFlow();

    // Open the popup synchronously, inside the click's user-activation, BEFORE any await.
    // Opening it after the PIN request resolves gets it blocked (slow backend, Android Chrome).
    let popup: Window | null = null;
    if (!redirectFlow) {
      const width = 600;
      const height = 700;
      const left = window.screenX + (window.outerWidth - width) / 2;
      const top = window.screenY + (window.outerHeight - height) / 2;
      popup = window.open(
        '',
        'plexAuthPopup',
        `width=${width},height=${height},left=${left},top=${top},status=0,toolbar=0,menubar=0`
      );
      if (popup) {
        try {
          popup.document.title = 'Plex sign-in';
          popup.document.body.style.cssText =
            'background:#0a0a0a;color:#a3a3a3;font:14px monospace;display:flex;align-items:center;justify-content:center;height:100vh;margin:0';
          popup.document.body.textContent = 'Connecting to Plex...';
        } catch {
          // Placeholder is cosmetic; leave the blank page.
        }
      }
    }
    popupRef.current = popup;
    setPlexAuthPhase(redirectFlow ? 'redirecting' : popup ? 'popup' : 'starting');

    try {
      const forwardUrl = redirectFlow
        ? `${window.location.origin}${window.location.pathname}`
        : undefined;
      const pinData = await startPlexAuth(forwardUrl);
      const authUrl = pinData.auth_url;
      const pinId = pinData.id;

      if (redirectFlow) {
        storePendingPin(pinId);
        window.location.assign(authUrl);
        return;
      }

      if (popup && !popup.closed) {
        popup.location.href = authUrl;
        setPlexAuthPhase('popup');
      } else {
        // Popup blocked (or closed already): do not poll blindly; ask for a fresh tap.
        popupRef.current = null;
        setPlexAuthUrl(authUrl);
        setPlexAuthPhase('blocked');
      }
      pollPin(pinId, POLL_MAX_ATTEMPTS);
    } catch (err: unknown) {
      if (popup && !popup.closed) popup.close();
      popupRef.current = null;
      const msg = err instanceof Error ? err.message : 'Failed to initialize Plex OAuth';
      setAuthError(msg);
      setPlexAuthPhase('idle');
      setIsAuthenticating(false);
    }
  }, [pollPin]);

  // Resume a redirect-based sign-in after returning from plex.tv.
  useEffect(() => {
    const pinId = readPendingPin();
    if (pinId === null) return;
    setIsAuthenticating(true);
    setPlexAuthPhase('redirecting');
    pollPin(pinId, 30);
  }, [pollPin]);

  const completeLocalSignIn = useCallback(
    async (opts: { mfaEnrollmentRequired: boolean }) => {
      setMfaEnrollmentRequired(opts.mfaEnrollmentRequired);
      await refreshUser();
    },
    [refreshUser]
  );

  const logout = useCallback(async () => {
    setMfaEnrollmentRequired(false);
    setIsLoading(true);
    try {
      await apiLogout();
    } finally {
      setUser(null);
      setToken(null);
      setIsLoading(false);
    }
  }, []);

  const tier: DeploymentTier = user?.tier ?? 'all-in-one';
  const isAdmin = Boolean(user?.is_admin);

  return {
    user,
    token,
    isAuthenticated: Boolean(user),
    isLoading,
    isAdmin,
    tier,
    canUseAdminUi: isAdmin && tier !== 'gateway',
    isAuthenticating,
    authError,
    loginWithPlex,
    plexAuthPhase,
    plexAuthUrl,
    cancelLogin,
    logout,
    refreshUser,
    mfaEnrollmentRequired,
    completeLocalSignIn,
  };
}
