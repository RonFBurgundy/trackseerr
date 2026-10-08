import { useCallback, useEffect, useState } from 'react';
import {
  getPushConfig,
  subscribePush,
  testPush,
  unsubscribePush,
} from '@/services/userNotificationsService';
import { errorMessage } from '@/services/apiClient';

function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

export interface UseUserWebPushReturn {
  isSupported: boolean;
  isSecureContext: boolean;
  permission: NotificationPermission | 'unsupported';
  isSubscribed: boolean;
  loading: boolean;
  testing: boolean;
  error: string | null;
  serverEnabled: boolean;
  subscribe: () => Promise<boolean>;
  unsubscribe: () => Promise<boolean>;
  sendTest: () => Promise<void>;
}

export function useUserWebPush(
  enabled: boolean,
  onToast?: (msg: string, tone?: 'ok' | 'error') => void
): UseUserWebPushReturn {
  const isSecure = typeof window !== 'undefined' ? Boolean(window.isSecureContext) : false;
  const isSupported =
    typeof window !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    'Notification' in window;

  const [permission, setPermission] = useState<NotificationPermission | 'unsupported'>(() => {
    if (!isSupported) return 'unsupported';
    return Notification.permission;
  });
  const [isSubscribed, setIsSubscribed] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(false);
  const [testing, setTesting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [serverEnabled, setServerEnabled] = useState<boolean>(true);

  // Check initial subscription state
  useEffect(() => {
    if (!enabled || !isSupported || !isSecure) return;

    let cancelled = false;

    async function checkSubscription(): Promise<void> {
      try {
        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.getSubscription();
        if (!cancelled) {
          setIsSubscribed(sub !== null);
          setPermission(Notification.permission);
        }
      } catch (err: unknown) {
        if (!cancelled) {
          console.debug('Error checking push subscription:', err);
        }
      }
    }

    void checkSubscription();

    return () => {
      cancelled = true;
    };
  }, [enabled, isSupported, isSecure]);

  const subscribe = useCallback(async (): Promise<boolean> => {
    if (!isSupported) {
      setError('Push notifications are not supported in this browser');
      return false;
    }
    if (!isSecure) {
      setError('Push notifications require a secure HTTPS connection');
      return false;
    }

    setLoading(true);
    setError(null);

    try {
      const perm = await Notification.requestPermission();
      setPermission(perm);
      if (perm !== 'granted') {
        const msg =
          perm === 'denied'
            ? 'Notification permission was denied. Please update your browser site settings.'
            : 'Notification permission was dismissed.';
        setError(msg);
        onToast?.(msg, 'error');
        return false;
      }

      const config = await getPushConfig();
      setServerEnabled(config.enabled);
      if (!config.enabled || !config.public_key) {
        throw new Error('Web Push is not enabled or configured on the server');
      }

      const reg = await navigator.serviceWorker.ready;
      const appKey = urlBase64ToUint8Array(config.public_key);
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: appKey.buffer as ArrayBuffer,
      });

      const json = sub.toJSON();
      const p256dh = json.keys?.p256dh;
      const auth = json.keys?.auth;

      if (!p256dh || !auth) {
        throw new Error('Push subscription keys could not be generated');
      }

      await subscribePush({
        endpoint: sub.endpoint,
        keys: { p256dh, auth },
        user_agent: typeof navigator !== 'undefined' ? navigator.userAgent : null,
      });

      setIsSubscribed(true);
      onToast?.('Web Push notifications enabled on this device');
      return true;
    } catch (err: unknown) {
      const msg = errorMessage(err, 'Failed to enable Web Push');
      setError(msg);
      onToast?.(msg, 'error');
      return false;
    } finally {
      setLoading(false);
    }
  }, [isSupported, isSecure, onToast]);

  const unsubscribe = useCallback(async (): Promise<boolean> => {
    if (!isSupported) return false;

    setLoading(true);
    setError(null);

    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        try {
          await unsubscribePush({ endpoint: sub.endpoint });
        } catch (err: unknown) {
          console.warn('Backend unsubscribe failed:', err);
        }
        await sub.unsubscribe();
      }
      setIsSubscribed(false);
      onToast?.('Web Push notifications disabled on this device');
      return true;
    } catch (err: unknown) {
      const msg = errorMessage(err, 'Failed to disable Web Push');
      setError(msg);
      onToast?.(msg, 'error');
      return false;
    } finally {
      setLoading(false);
    }
  }, [isSupported, onToast]);

  const sendTest = useCallback(async (): Promise<void> => {
    setTesting(true);
    try {
      const res = await testPush();
      onToast?.(res.message, res.success ? 'ok' : 'error');
    } catch (err: unknown) {
      const msg = errorMessage(err, 'Failed to send test push');
      onToast?.(msg, 'error');
    } finally {
      setTesting(false);
    }
  }, [onToast]);

  return {
    isSupported,
    isSecureContext: isSecure,
    permission,
    isSubscribed,
    loading,
    testing,
    error,
    serverEnabled,
    subscribe,
    unsubscribe,
    sendTest,
  };
}
