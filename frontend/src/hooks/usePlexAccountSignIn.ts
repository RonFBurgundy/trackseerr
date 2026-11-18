import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from '@/services/apiClient';
import { checkPlexAccountSignIn, startPlexAccountSignIn } from '@/services/mediaServerService';

export type PlexAccountSignInPhase = 'idle' | 'waiting' | 'blocked' | 'error';

const POLL_INTERVAL_MS = 2000;
const POLL_MAX_ATTEMPTS = 60; // 2 minutes

export interface UsePlexAccountSignInReturn {
  phase: PlexAccountSignInPhase;
  authUrl: string | null;
  error: string | null;
  start: () => Promise<void>;
  cancel: () => void;
}

/**
 * Popup + polling flow for linking the Plex server owner account on the Media Server settings card.
 * Opens the popup synchronously on click before awaiting the PIN request so browsers do not block it.
 */
export function usePlexAccountSignIn(onConnected: (username: string) => void): UsePlexAccountSignInReturn {
  const [phase, setPhase] = useState<PlexAccountSignInPhase>('idle');
  const [authUrl, setAuthUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const pollTimerRef = useRef<number | null>(null);
  const popupRef = useRef<Window | null>(null);
  const onConnectedRef = useRef(onConnected);
  onConnectedRef.current = onConnected;

  const stopPollingAndClosePopup = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (popupRef.current && !popupRef.current.closed) {
      popupRef.current.close();
    }
    popupRef.current = null;
  }, []);

  const cancel = useCallback(() => {
    stopPollingAndClosePopup();
    setPhase('idle');
    setAuthUrl(null);
    setError(null);
  }, [stopPollingAndClosePopup]);

  useEffect(() => {
    return () => {
      stopPollingAndClosePopup();
    };
  }, [stopPollingAndClosePopup]);

  const pollPin = useCallback(
    (pinId: number) => {
      let attempts = 0;
      if (pollTimerRef.current !== null) window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = window.setInterval(async () => {
        attempts++;
        if (popupRef.current && popupRef.current.closed) {
          cancel();
          return;
        }
        if (attempts > POLL_MAX_ATTEMPTS) {
          stopPollingAndClosePopup();
          setAuthUrl(null);
          setError('Plex sign-in timed out. Please try again.');
          setPhase('error');
          return;
        }
        try {
          const res = await checkPlexAccountSignIn(pinId);
          if (res.connected) {
            stopPollingAndClosePopup();
            setPhase('idle');
            setAuthUrl(null);
            setError(null);
            onConnectedRef.current(res.username ?? '');
            return;
          }
          if (!res.pending) {
            stopPollingAndClosePopup();
            setAuthUrl(null);
            setError('Plex sign-in did not complete. Please try again.');
            setPhase('error');
          }
        } catch (err: unknown) {
          stopPollingAndClosePopup();
          setAuthUrl(null);
          setError(errorMessage(err, 'Failed to verify Plex sign-in'));
          setPhase('error');
        }
      }, POLL_INTERVAL_MS);
    },
    [cancel, stopPollingAndClosePopup]
  );

  const start = useCallback(async () => {
    stopPollingAndClosePopup();
    setError(null);
    setAuthUrl(null);

    const width = 600;
    const height = 700;
    const left = window.screenX + (window.outerWidth - width) / 2;
    const top = window.screenY + (window.outerHeight - height) / 2;
    const popup = window.open(
      '',
      'plexAccountSignInPopup',
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
    popupRef.current = popup;
    setPhase('waiting');

    try {
      const pinData = await startPlexAccountSignIn();
      if (popup && !popup.closed) {
        popup.location.href = pinData.auth_url;
        setPhase('waiting');
      } else {
        popupRef.current = null;
        setAuthUrl(pinData.auth_url);
        setPhase('blocked');
      }
      pollPin(pinData.id);
    } catch (err: unknown) {
      if (popup && !popup.closed) popup.close();
      popupRef.current = null;
      setError(errorMessage(err, 'Failed to start Plex sign-in'));
      setPhase('error');
    }
  }, [pollPin, stopPollingAndClosePopup]);

  return { phase, authUrl, error, start, cancel };
}
