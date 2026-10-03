import { useState, useEffect, useCallback, useRef } from 'react';
import type { DeploymentTier, User } from '@/types/models';
import {
  getCurrentUser,
  startPlexAuth,
  verifyPin,
  logout as apiLogout,
} from '@/services/authService';
import { getAuthToken, setAuthToken } from '@/services/apiClient';

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

  const cancelLogin = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (popupRef.current && !popupRef.current.closed) {
      popupRef.current.close();
      popupRef.current = null;
    }
    setIsAuthenticating(false);
  }, []);

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

    try {
      const pinData = await startPlexAuth();
      const authUrl = pinData.auth_url;
      const pinId = pinData.id;

      // Open OAuth popup centered
      const width = 600;
      const height = 700;
      const left = window.screenX + (window.outerWidth - width) / 2;
      const top = window.screenY + (window.outerHeight - height) / 2;

      popupRef.current = window.open(
        authUrl,
        'plexAuthPopup',
        `width=${width},height=${height},left=${left},top=${top},status=0,toolbar=0,menubar=0`
      );

      let attempts = 0;
      const maxAttempts = 60; // 2 minutes (every 2s)

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
      }, 2000);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to initialize Plex OAuth';
      setAuthError(msg);
      setIsAuthenticating(false);
    }
  }, [cancelLogin]);

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
    cancelLogin,
    logout,
    refreshUser,
    mfaEnrollmentRequired,
    completeLocalSignIn,
  };
}
