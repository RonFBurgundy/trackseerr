import { useCallback, useState } from 'react';
import type { DownloadClientItem, DownloadDriverType } from '@/types/models';
import type { Schema } from '@/types/apiSchema';

export const DRIVER_TYPES: ReadonlyArray<DownloadDriverType> = [
  'slskd',
  'sabnzbd',
  'qbittorrent',
  'transmission',
  'deluge',
  'nzbget',
];

export const DEFAULT_URLS: Record<DownloadDriverType, string> = {
  slskd: 'http://localhost:5030',
  sabnzbd: 'http://localhost:8080',
  qbittorrent: 'http://localhost:8080',
  transmission: 'http://localhost:9091',
  deluge: 'http://localhost:8112',
  nzbget: 'http://localhost:6789',
  lidarr: 'http://localhost:8686',
};

export const DEFAULT_CATEGORIES: Partial<Record<DownloadDriverType, string>> = {
  sabnzbd: 'music',
  qbittorrent: 'trackseerr',
  transmission: 'trackseerr',
  deluge: 'trackseerr',
  nzbget: 'music',
};

const DEFAULT_RPC_PATH = '/transmission/rpc';

export function toDriverType(value: string): DownloadDriverType {
  return DRIVER_TYPES.find((t) => t === value) ?? 'slskd';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Reads `rpc_path` out of a client's `extra_settings_json`; '' when absent or unparsable. */
function parseRpcPath(extra: string | null | undefined): string {
  if (!extra) return '';
  try {
    const parsed: unknown = JSON.parse(extra);
    if (isRecord(parsed) && typeof parsed.rpc_path === 'string') return parsed.rpc_path;
  } catch {
    return '';
  }
  return '';
}

export interface ClientDraft {
  editingId: string | null;
  name: string;
  type: DownloadDriverType;
  hostUrl: string;
  rpcPath: string;
  username: string;
  password: string;
  apiKey: string;
  category: string;
  setName: (v: string) => void;
  setType: (v: DownloadDriverType) => void;
  setHostUrl: (v: string) => void;
  setRpcPath: (v: string) => void;
  setUsername: (v: string) => void;
  setPassword: (v: string) => void;
  setApiKey: (v: string) => void;
  setCategory: (v: string) => void;
  startEdit: (client: DownloadClientItem) => void;
  reset: () => void;
  toPayload: () => Schema<'DownloadClientPayload'>;
}

/** Form state for adding or editing a download client, with per-driver credential rules. */
export function useClientDraft(): ClientDraft {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState<string>('');
  const [type, setTypeState] = useState<DownloadDriverType>('slskd');
  const [hostUrl, setHostUrl] = useState<string>(DEFAULT_URLS.slskd);
  const [rpcPath, setRpcPath] = useState<string>(DEFAULT_RPC_PATH);
  const [username, setUsername] = useState<string>('');
  const [password, setPassword] = useState<string>('');
  const [apiKey, setApiKey] = useState<string>('');
  const [category, setCategory] = useState<string>('');

  const setType = useCallback((next: DownloadDriverType) => {
    setTypeState(next);
    setHostUrl(DEFAULT_URLS[next]);
    setCategory(DEFAULT_CATEGORIES[next] ?? '');
  }, []);

  const startEdit = useCallback((client: DownloadClientItem) => {
    setEditingId(client.id);
    setName(client.name);
    setTypeState(client.driver_type);
    setHostUrl(client.host_url);
    setRpcPath(parseRpcPath(client.extra_settings_json) || DEFAULT_RPC_PATH);
    setUsername(client.username ?? '');
    setPassword(client.password ?? '');
    setApiKey(client.api_key ?? '');
    setCategory(client.category ?? '');
  }, []);

  const reset = useCallback(() => {
    setEditingId(null);
    setName('');
    setTypeState('slskd');
    setHostUrl(DEFAULT_URLS.slskd);
    setRpcPath(DEFAULT_RPC_PATH);
    setUsername('');
    setPassword('');
    setApiKey('');
    setCategory('');
  }, []);

  const toPayload = useCallback((): Schema<'DownloadClientPayload'> => {
    const extraSettings: Record<string, string> = {};
    if (type === 'transmission' && rpcPath.trim()) extraSettings.rpc_path = rpcPath.trim();
    if (category.trim()) extraSettings.category = category.trim();

    const usesUsername = type === 'slskd' || type === 'qbittorrent' || type === 'transmission' || type === 'nzbget';
    const usesPassword = usesUsername || type === 'deluge';
    const usesApiKey = type === 'slskd' || type === 'sabnzbd';

    return {
      ...(editingId ? { id: editingId } : {}),
      name: name.trim(),
      driver_type: type,
      host_url: hostUrl.trim(),
      enabled: true,
      priority: 1,
      username: usesUsername && username.trim() ? username.trim() : undefined,
      password: usesPassword && password.trim() ? password.trim() : undefined,
      api_key: usesApiKey && apiKey.trim() ? apiKey.trim() : undefined,
      category: category.trim() || undefined,
      extra_settings_json: Object.keys(extraSettings).length > 0 ? JSON.stringify(extraSettings) : undefined,
    };
  }, [editingId, name, type, hostUrl, rpcPath, username, password, apiKey, category]);

  return {
    editingId, name, type, hostUrl, rpcPath, username, password, apiKey, category,
    setName, setType, setHostUrl, setRpcPath, setUsername, setPassword, setApiKey, setCategory,
    startEdit, reset, toPayload,
  };
}
