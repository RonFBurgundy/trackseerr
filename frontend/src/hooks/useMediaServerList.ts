import { useCallback, useEffect, useState } from 'react';
import type { MediaServerItem, MediaServerKind, MediaServerPayload } from '@/types/mediaServer';
import { errorMessage } from '@/services/apiClient';
import { listMediaServers } from '@/services/mediaServerService';

export interface MediaServerListState {
  servers: MediaServerItem[];
  isLoading: boolean;
  error: string | null;
  reload: () => Promise<void>;
  editingId: string | null;
  kind: MediaServerKind;
  name: string;
  url: string;
  username: string;
  password: string;
  apiKey: string;
  musicSection: string;
  enabled: boolean;
  useRefresh: boolean;
  usePlaylists: boolean;
  useUsers: boolean;
  useScrobbles: boolean;
  setKind: (v: MediaServerKind) => void;
  setName: (v: string) => void;
  setUrl: (v: string) => void;
  setUsername: (v: string) => void;
  setPassword: (v: string) => void;
  setApiKey: (v: string) => void;
  setMusicSection: (v: string) => void;
  setEnabled: (v: boolean) => void;
  setUseRefresh: (v: boolean) => void;
  setUsePlaylists: (v: boolean) => void;
  setUseUsers: (v: boolean) => void;
  setUseScrobbles: (v: boolean) => void;
  startEdit: (item: MediaServerItem) => void;
  reset: () => void;
  toPayload: (editing: MediaServerItem | null) => MediaServerPayload;
}

/** Loads configured media servers and manages the add/edit form draft. */
export function useMediaServerList(): MediaServerListState {
  const [servers, setServers] = useState<MediaServerItem[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [kind, setKind] = useState<MediaServerKind>('plex');
  const [name, setName] = useState<string>('');
  const [url, setUrl] = useState<string>('');
  const [username, setUsername] = useState<string>('');
  const [password, setPassword] = useState<string>('');
  const [apiKey, setApiKey] = useState<string>('');
  const [musicSection, setMusicSection] = useState<string>('');
  const [enabled, setEnabled] = useState<boolean>(true);
  const [useRefresh, setUseRefresh] = useState<boolean>(true);
  const [usePlaylists, setUsePlaylists] = useState<boolean>(true);
  const [useUsers, setUseUsers] = useState<boolean>(true);
  const [useScrobbles, setUseScrobbles] = useState<boolean>(true);

  const reload = useCallback(async () => {
    try {
      const next = await listMediaServers();
      setServers(next);
      setError(null);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not load media servers'));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    listMediaServers()
      .then((next) => {
        if (cancelled) return;
        setServers(next);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorMessage(err, 'Could not load media servers'));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const startEdit = useCallback((item: MediaServerItem) => {
    setEditingId(item.id);
    setKind(item.kind);
    setName(item.name);
    setUrl(item.url);
    setUsername(item.username);
    setPassword(item.password);
    setApiKey(item.api_key);
    setMusicSection(item.music_section);
    setEnabled(item.enabled);
    setUseRefresh(item.use_refresh);
    setUsePlaylists(item.use_playlists);
    setUseUsers(item.use_users);
    setUseScrobbles(item.use_scrobbles);
  }, []);

  const reset = useCallback(() => {
    setEditingId(null);
    setKind('plex');
    setName('');
    setUrl('');
    setUsername('');
    setPassword('');
    setApiKey('');
    setMusicSection('');
    setEnabled(true);
    setUseRefresh(true);
    setUsePlaylists(true);
    setUseUsers(true);
    setUseScrobbles(true);
  }, []);

  const toPayload = useCallback(
    (editing: MediaServerItem | null): MediaServerPayload => ({
      ...(editing ? { id: editing.id } : {}),
      kind,
      name: name.trim(),
      url: url.trim(),
      username: username.trim(),
      password,
      api_key: apiKey,
      music_section: musicSection.trim(),
      verify_ssl: editing ? editing.verify_ssl : true,
      enabled,
      use_refresh: useRefresh,
      use_playlists: usePlaylists,
      use_users: useUsers,
      use_scrobbles: useScrobbles,
      sort_order: editing ? editing.sort_order : 0,
    }),
    [kind, name, url, username, password, apiKey, musicSection, enabled, useRefresh, usePlaylists, useUsers, useScrobbles]
  );

  return {
    servers,
    isLoading,
    error,
    reload,
    editingId,
    kind,
    name,
    url,
    username,
    password,
    apiKey,
    musicSection,
    enabled,
    useRefresh,
    usePlaylists,
    useUsers,
    useScrobbles,
    setKind,
    setName,
    setUrl,
    setUsername,
    setPassword,
    setApiKey,
    setMusicSection,
    setEnabled,
    setUseRefresh,
    setUsePlaylists,
    setUseUsers,
    setUseScrobbles,
    startEdit,
    reset,
    toPayload,
  };
}
