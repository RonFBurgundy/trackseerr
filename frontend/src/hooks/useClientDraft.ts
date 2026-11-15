import { useCallback, useState } from 'react';
import type { DownloadClientItem, DownloadDriverType } from '@/types/models';
import type { Schema } from '@/types/apiSchema';

export const DRIVER_TYPES: ReadonlyArray<DownloadDriverType> = [
  'slskd',
  'sabnzbd',
  'qbittorrent',
  'transmission',
  'deluge',
  'rtorrent',
  'nzbget',
];

export const DEFAULT_URLS: Record<DownloadDriverType, string> = {
  slskd: 'http://localhost:5030',
  sabnzbd: 'http://localhost:8080',
  qbittorrent: 'http://localhost:8080',
  transmission: 'http://localhost:9091',
  deluge: 'http://localhost:8112',
  rtorrent: 'http://localhost:8080',
  nzbget: 'http://localhost:6789',
  lidarr: 'http://localhost:8686',
};

export const DEFAULT_CATEGORIES: Partial<Record<DownloadDriverType, string>> = {
  sabnzbd: 'music',
  qbittorrent: 'trackseerr',
  transmission: 'trackseerr',
  deluge: 'trackseerr',
  rtorrent: 'trackseerr',
  nzbget: 'music',
};

const DEFAULT_RPC_PATHS: Partial<Record<DownloadDriverType, string>> = {
  transmission: '/transmission/rpc',
  rtorrent: '/RPC2',
};

/** Default RPC path for a driver; '' for drivers that take no RPC path. */
export function defaultRpcPath(t: DownloadDriverType): string {
  return DEFAULT_RPC_PATHS[t] ?? '';
}

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

/** Parses `extra_settings_json` into an object; {} when absent or unparsable. */
function parseExtra(extra: string | null | undefined): Record<string, unknown> {
  if (!extra) return {};
  try {
    const parsed: unknown = JSON.parse(extra);
    return isRecord(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

export const SLSKD_NUMERIC_KEYS = [
  'max_peer_queue',
  'min_upload_speed_kbps',
  'search_timeout',
  'min_search_interval',
  'queue_timeout_minutes',
  'stall_minutes',
  'missing_minutes',
] as const;

export type SlskdNumericKey = (typeof SLSKD_NUMERIC_KEYS)[number];
export type SlskdTuningKey = SlskdNumericKey | 'ignored_users';
/** Form strings for the slskd tuning keys; blank means "omit, backend default applies". */
export type SlskdTuning = Record<SlskdTuningKey, string>;

export const EMPTY_SLSKD_TUNING: SlskdTuning = {
  max_peer_queue: '',
  min_upload_speed_kbps: '',
  search_timeout: '',
  min_search_interval: '',
  queue_timeout_minutes: '',
  stall_minutes: '',
  missing_minutes: '',
  ignored_users: '',
};

function readSlskdTuning(extra: Record<string, unknown>): SlskdTuning {
  const out: SlskdTuning = { ...EMPTY_SLSKD_TUNING };
  for (const key of SLSKD_NUMERIC_KEYS) {
    const v = extra[key];
    if (typeof v === 'number' && Number.isFinite(v)) out[key] = String(v);
    else if (typeof v === 'string' && v.trim()) out[key] = v.trim();
  }
  const ignored = extra.ignored_users;
  if (Array.isArray(ignored)) {
    out.ignored_users = ignored.filter((u): u is string => typeof u === 'string').join(', ');
  } else if (typeof ignored === 'string') {
    out.ignored_users = ignored;
  }
  return out;
}

export interface PathMapping {
  remote_path: string;
  local_path: string;
}

function readPathMappings(extra: Record<string, unknown>): PathMapping[] {
  const raw = extra.remote_path_mappings;
  if (!Array.isArray(raw)) return [];
  const out: PathMapping[] = [];
  for (const entry of raw) {
    if (isRecord(entry) && typeof entry.remote_path === 'string' && typeof entry.local_path === 'string') {
      out.push({ remote_path: entry.remote_path, local_path: entry.local_path });
    }
  }
  return out;
}

export const DEFAULT_CLIENT_PRIORITY = 1;

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
  priority: number;
  enabled: boolean;
  slskd: SlskdTuning;
  pathMappings: PathMapping[];
  setName: (v: string) => void;
  setType: (v: DownloadDriverType) => void;
  setHostUrl: (v: string) => void;
  setRpcPath: (v: string) => void;
  setUsername: (v: string) => void;
  setPassword: (v: string) => void;
  setApiKey: (v: string) => void;
  setCategory: (v: string) => void;
  setPriority: (v: number) => void;
  setEnabled: (v: boolean) => void;
  setSlskdField: (key: SlskdTuningKey, value: string) => void;
  addPathMapping: () => void;
  updatePathMapping: (index: number, key: keyof PathMapping, value: string) => void;
  removePathMapping: (index: number) => void;
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
  const [rpcPath, setRpcPath] = useState<string>(defaultRpcPath('slskd'));
  const [username, setUsername] = useState<string>('');
  const [password, setPassword] = useState<string>('');
  const [apiKey, setApiKey] = useState<string>('');
  const [category, setCategory] = useState<string>('');
  const [priority, setPriority] = useState<number>(DEFAULT_CLIENT_PRIORITY);
  const [enabled, setEnabled] = useState<boolean>(true);
  const [slskd, setSlskd] = useState<SlskdTuning>(EMPTY_SLSKD_TUNING);
  const [pathMappings, setPathMappings] = useState<PathMapping[]>([]);
  // Original parsed extra settings; edits merge over it so keys the form does not know survive a save.
  const [baseExtra, setBaseExtra] = useState<Record<string, unknown>>({});

  const setSlskdField = useCallback((key: SlskdTuningKey, value: string) => {
    setSlskd((prev) => ({ ...prev, [key]: value }));
  }, []);

  const addPathMapping = useCallback(() => {
    setPathMappings((prev) => [...prev, { remote_path: '', local_path: '' }]);
  }, []);

  const updatePathMapping = useCallback((index: number, key: keyof PathMapping, value: string) => {
    setPathMappings((prev) => prev.map((row, i) => (i === index ? { ...row, [key]: value } : row)));
  }, []);

  const removePathMapping = useCallback((index: number) => {
    setPathMappings((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const setType = useCallback((next: DownloadDriverType) => {
    setTypeState(next);
    setHostUrl(DEFAULT_URLS[next]);
    setRpcPath(defaultRpcPath(next));
    setCategory(DEFAULT_CATEGORIES[next] ?? '');
  }, []);

  const startEdit = useCallback((client: DownloadClientItem) => {
    setEditingId(client.id);
    setName(client.name);
    setTypeState(client.driver_type);
    setHostUrl(client.host_url);
    setRpcPath(parseRpcPath(client.extra_settings_json) || defaultRpcPath(client.driver_type));
    setUsername(client.username ?? '');
    setPassword(client.password ?? '');
    setApiKey(client.api_key ?? '');
    setCategory(client.category ?? '');
    setPriority(client.priority);
    setEnabled(client.enabled);
    const extra = parseExtra(client.extra_settings_json);
    setBaseExtra(extra);
    setSlskd(readSlskdTuning(extra));
    setPathMappings(readPathMappings(extra));
  }, []);

  const reset = useCallback(() => {
    setEditingId(null);
    setName('');
    setTypeState('slskd');
    setHostUrl(DEFAULT_URLS.slskd);
    setRpcPath(defaultRpcPath('slskd'));
    setUsername('');
    setPassword('');
    setApiKey('');
    setCategory('');
    setPriority(DEFAULT_CLIENT_PRIORITY);
    setEnabled(true);
    setBaseExtra({});
    setSlskd(EMPTY_SLSKD_TUNING);
    setPathMappings([]);
  }, []);

  const toPayload = useCallback((): Schema<'DownloadClientPayload'> => {
    const extraSettings: Record<string, unknown> = { ...baseExtra };
    if ((type === 'transmission' || type === 'rtorrent') && rpcPath.trim()) extraSettings.rpc_path = rpcPath.trim();
    else delete extraSettings.rpc_path;
    if (category.trim()) extraSettings.category = category.trim();
    else delete extraSettings.category;
    if (type === 'slskd') {
      for (const key of SLSKD_NUMERIC_KEYS) {
        const raw = slskd[key].trim();
        const n = raw === '' ? NaN : Number(raw);
        if (Number.isFinite(n)) extraSettings[key] = n;
        else delete extraSettings[key];
      }
      const ignored = slskd.ignored_users.trim();
      if (ignored) extraSettings.ignored_users = ignored;
      else delete extraSettings.ignored_users;
    }
    const mappingRows = pathMappings
      .map((m) => ({ remote_path: m.remote_path.trim(), local_path: m.local_path.trim() }))
      .filter((m) => m.remote_path !== '' && m.local_path !== '');
    if (mappingRows.length > 0) extraSettings.remote_path_mappings = mappingRows;
    else delete extraSettings.remote_path_mappings;

    const usesUsername = type === 'qbittorrent' || type === 'transmission' || type === 'rtorrent' || type === 'nzbget';
    const usesPassword = usesUsername || type === 'deluge';
    const usesApiKey = type === 'slskd' || type === 'sabnzbd';

    return {
      ...(editingId ? { id: editingId } : {}),
      name: name.trim(),
      driver_type: type,
      host_url: hostUrl.trim(),
      enabled,
      priority,
      username: usesUsername && username.trim() ? username.trim() : undefined,
      password: usesPassword && password.trim() ? password.trim() : undefined,
      api_key: usesApiKey && apiKey.trim() ? apiKey.trim() : undefined,
      category: category.trim() || undefined,
      extra_settings_json: Object.keys(extraSettings).length > 0 ? JSON.stringify(extraSettings) : undefined,
    };
  }, [editingId, name, type, hostUrl, rpcPath, username, password, apiKey, category, priority, enabled, slskd, pathMappings, baseExtra]);

  return {
    editingId, name, type, hostUrl, rpcPath, username, password, apiKey, category, priority, enabled, slskd, pathMappings,
    setName, setType, setHostUrl, setRpcPath, setUsername, setPassword, setApiKey, setCategory, setPriority, setEnabled, setSlskdField,
    addPathMapping, updatePathMapping, removePathMapping,
    startEdit, reset, toPayload,
  };
}
