import { useState, useEffect, useCallback } from 'react';
import type {
  AdminScrobbleConfigBody,
  ScrobbleConfig,
  ScrobbleConfigBody,
  ScrobbleServerConfig,
  ScrobbleServerConfigBody,
  UserListen,
} from '@/types/models';
import {
  getScrobbleConfig,
  updateScrobbleConfig,
  getListens,
  getLastfmAuthUrl,
  getScrobbleUsers,
  updateUserScrobbleConfig,
  getScrobbleServerConfig,
  updateScrobbleServerConfig,
  getWebhookUrl,
  rotateWebhookSecret,
  completeLastfm,
} from '@/services/scrobbleService';
import { ApiError } from '@/services/apiClient';

export type ScrobbleNoticeKind = 'success' | 'error';

export interface ScrobbleNotice {
  kind: ScrobbleNoticeKind;
  message: string;
}

export interface UseScrobblingReturn {
  config: ScrobbleConfig | null;
  listens: UserListen[];
  isLoading: boolean;
  isSaving: boolean;
  lastfmUnavailable: boolean;
  notice: ScrobbleNotice | null;
  dismissNotice: () => void;
  connectLastfm: () => Promise<void>;
  disconnectLastfm: () => Promise<void>;
  setScrobblingEnabled: (enabled: boolean) => Promise<void>;
  saveListenBrainzToken: (token: string) => Promise<boolean>;
  unlinkListenBrainz: () => Promise<void>;
  refreshListens: () => Promise<void>;
  // Admin
  serverConfig: ScrobbleServerConfig | null;
  webhookUrl: string | null;
  users: ScrobbleConfig[];
  saveServerConfig: (body: ScrobbleServerConfigBody) => Promise<boolean>;
  rotateWebhook: () => Promise<void>;
  saveUserConfig: (userId: string, body: AdminScrobbleConfigBody) => Promise<boolean>;
}

const ERROR_MESSAGES: Record<string, string> = {
  state_user: 'That Last.fm connection was started by a different TrackSeerr account. Start it again from your own account.',
  state: 'This Last.fm connection link is unknown or was already used. Start the connection again.',
  state_expired: 'The Last.fm connection took longer than 10 minutes. Start the connection again.',
  user: 'Your account could not be found or is disabled, so Last.fm was not connected.',
  lastfm: 'Last.fm rejected the connection. Please try again.',
};

/** Reads the Last.fm return params, then strips them from the URL so the token never lingers there. */
function consumeLastfmParams(): { state: string; token: string } | null {
  if (typeof window === 'undefined') return null;
  const url = new URL(window.location.href);
  const state = url.searchParams.get('lastfm_state');
  const token = url.searchParams.get('lastfm_token');
  if (state === null && token === null) return null;
  url.searchParams.delete('lastfm_state');
  url.searchParams.delete('lastfm_token');
  const qs = url.searchParams.toString();
  window.history.replaceState(null, '', `${url.pathname}${qs ? `?${qs}` : ''}${url.hash}`);
  return state && token ? { state, token } : null;
}

/** The server's machine-readable `detail.reason` of a failed completion, if present. */
function completeReason(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  const detail = err.detail;
  if (typeof detail === 'object' && detail !== null && 'reason' in detail) {
    const reason = (detail as { reason?: unknown }).reason;
    return typeof reason === 'string' ? reason : null;
  }
  return null;
}

function errMsg(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

/** Reads `connected` / `scrobble_error` from the URL, then strips them. */
function consumeUrlNotice(): ScrobbleNotice | null {
  if (typeof window === 'undefined') return null;
  const url = new URL(window.location.href);
  const connected = url.searchParams.get('connected');
  const error = url.searchParams.get('scrobble_error');
  if (connected === null && error === null) return null;

  url.searchParams.delete('connected');
  url.searchParams.delete('scrobble_error');
  const qs = url.searchParams.toString();
  window.history.replaceState(null, '', `${url.pathname}${qs ? `?${qs}` : ''}${url.hash}`);

  if (error !== null) {
    return { kind: 'error', message: ERROR_MESSAGES[error] ?? 'Scrobbling connection failed.' };
  }
  if (connected === 'lastfm') {
    return { kind: 'success', message: 'Last.fm connected. Your listens will now be scrobbled.' };
  }
  return null;
}

export function useScrobbling(isAdmin: boolean): UseScrobblingReturn {
  const [config, setConfig] = useState<ScrobbleConfig | null>(null);
  const [listens, setListens] = useState<UserListen[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [lastfmUnavailable, setLastfmUnavailable] = useState<boolean>(false);
  const [notice, setNotice] = useState<ScrobbleNotice | null>(null);

  const [serverConfig, setServerConfig] = useState<ScrobbleServerConfig | null>(null);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [users, setUsers] = useState<ScrobbleConfig[]>([]);

  const fail = useCallback((err: unknown, fallback: string) => {
    setNotice({ kind: 'error', message: errMsg(err, fallback) });
  }, []);

  useEffect(() => {
    const pending = consumeLastfmParams();
    if (pending) {
      // Return trip from last.fm: finish it under THIS browser session (the server checks the state is ours).
      void (async () => {
        try {
          await completeLastfm(pending.state, pending.token);
          setConfig(await getScrobbleConfig());
          setNotice({ kind: 'success', message: 'Last.fm connected. Your listens will now be scrobbled.' });
        } catch (err) {
          const reason = completeReason(err);
          setNotice({
            kind: 'error',
            message: (reason !== null ? ERROR_MESSAGES[reason] : undefined) ?? errMsg(err, 'Last.fm connection failed.'),
          });
        }
      })();
      return;
    }
    const urlNotice = consumeUrlNotice();
    if (urlNotice) setNotice(urlNotice);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setIsLoading(true);
      try {
        const [cfg, ls] = await Promise.all([getScrobbleConfig(), getListens(20, 0)]);
        if (cancelled) return;
        setConfig(cfg);
        setListens(ls);
      } catch (err) {
        if (!cancelled) fail(err, 'Failed to load scrobbling settings');
      }
      if (isAdmin) {
        const [srv, hook, usr] = await Promise.all([
          getScrobbleServerConfig().catch(() => null),
          getWebhookUrl().catch(() => null),
          getScrobbleUsers().catch(() => [] as ScrobbleConfig[]),
        ]);
        if (cancelled) return;
        setServerConfig(srv);
        setWebhookUrl(hook ? hook.url : null);
        setUsers(usr);
      }
      if (!cancelled) setIsLoading(false);
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [isAdmin, fail]);

  const dismissNotice = useCallback(() => setNotice(null), []);

  const refreshListens = useCallback(async () => {
    try {
      setListens(await getListens(20, 0));
    } catch (err) {
      fail(err, 'Failed to refresh listens');
    }
  }, [fail]);

  const applyConfig = useCallback(
    async (body: ScrobbleConfigBody, okMessage: string): Promise<boolean> => {
      setIsSaving(true);
      try {
        setConfig(await updateScrobbleConfig(body));
        setNotice({ kind: 'success', message: okMessage });
        return true;
      } catch (err) {
        fail(err, 'Failed to save scrobbling settings');
        return false;
      } finally {
        setIsSaving(false);
      }
    },
    [fail]
  );

  const connectLastfm = useCallback(async () => {
    setIsSaving(true);
    try {
      const forward = `${window.location.pathname}${window.location.search}`;
      const { url } = await getLastfmAuthUrl(forward);
      window.location.href = url;
    } catch (err) {
      const msg = errMsg(err, '');
      if (/not configured/i.test(msg)) {
        setLastfmUnavailable(true);
      } else {
        fail(err, 'Could not start Last.fm connection');
      }
      setIsSaving(false);
    }
  }, [fail]);

  const disconnectLastfm = useCallback(async () => {
    await applyConfig({ unlink_lastfm: true }, 'Last.fm disconnected');
  }, [applyConfig]);

  const setScrobblingEnabled = useCallback(
    async (enabled: boolean) => {
      await applyConfig(
        { scrobbling_enabled: enabled },
        enabled ? 'Scrobbling enabled' : 'Scrobbling paused'
      );
    },
    [applyConfig]
  );

  const saveListenBrainzToken = useCallback(
    (token: string) => applyConfig({ listenbrainz_token: token.trim() }, 'ListenBrainz connected'),
    [applyConfig]
  );

  const unlinkListenBrainz = useCallback(async () => {
    await applyConfig({ listenbrainz_token: null }, 'ListenBrainz disconnected');
  }, [applyConfig]);

  const saveServerConfig = useCallback(
    async (body: ScrobbleServerConfigBody): Promise<boolean> => {
      setIsSaving(true);
      try {
        await updateScrobbleServerConfig(body);
        setServerConfig(await getScrobbleServerConfig());
        setNotice({ kind: 'success', message: 'Server scrobbling settings saved' });
        return true;
      } catch (err) {
        fail(err, 'Failed to save server settings');
        return false;
      } finally {
        setIsSaving(false);
      }
    },
    [fail]
  );

  const rotateWebhook = useCallback(async () => {
    setIsSaving(true);
    try {
      const { url } = await rotateWebhookSecret();
      setWebhookUrl(url);
      setNotice({ kind: 'success', message: 'Webhook secret rotated. Update Plex with the new URL.' });
    } catch (err) {
      fail(err, 'Failed to rotate webhook secret');
    } finally {
      setIsSaving(false);
    }
  }, [fail]);

  const saveUserConfig = useCallback(
    async (userId: string, body: AdminScrobbleConfigBody): Promise<boolean> => {
      setIsSaving(true);
      try {
        const updated = await updateUserScrobbleConfig(userId, body);
        setUsers((prev) => prev.map((u) => (u.user_id === updated.user_id ? updated : u)));
        setConfig((prev) => (prev && prev.user_id === updated.user_id ? updated : prev));
        setNotice({ kind: 'success', message: `Updated ${updated.username}` });
        return true;
      } catch (err) {
        fail(err, 'Failed to update user');
        return false;
      } finally {
        setIsSaving(false);
      }
    },
    [fail]
  );

  return {
    config,
    listens,
    isLoading,
    isSaving,
    lastfmUnavailable,
    notice,
    dismissNotice,
    connectLastfm,
    disconnectLastfm,
    setScrobblingEnabled,
    saveListenBrainzToken,
    unlinkListenBrainz,
    refreshListens,
    serverConfig,
    webhookUrl,
    users,
    saveServerConfig,
    rotateWebhook,
    saveUserConfig,
  };
}
