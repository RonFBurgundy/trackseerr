import { settingsRouteFor } from '@/hooks/useAppRoute';
import type { AppRoute, SettingsSection } from '@/hooks/useAppRoute';
import { resolveSettingsRoute } from '@/components/settings/settingsTabs';
import { buildNavTree, routesEqual } from './navTree';
import type { NavNode, RouteAccess } from './navTree';

/** One searchable destination: a page, or a single setting that lives inside a page. */
export interface NavSearchEntry {
  /** Stable unique key. */
  key: string;
  label: string;
  /** Path shown under the label, e.g. `['Settings', 'Media Management', 'Write Tags']` (includes the label last). */
  breadcrumb: readonly string[];
  route: AppRoute;
  /** Extra words that should find this entry (synonyms, related terms). */
  keywords: readonly string[];
  /** For a setting: highlight target on the destination page. */
  target?: SettingTarget;
}

/** How to find a setting on its page. A static element id wins; otherwise the visible text is matched. */
export interface SettingTarget {
  elementId?: string;
  /** Visible text of the label or heading (case-insensitive prefix match). Defaults to the entry label. */
  anchor?: string;
}

interface SettingDef {
  label: string;
  section: SettingsSection;
  leaf?: string;
  keywords: string;
  elementId?: string;
  anchor?: string;
  /** True for controls only admins see, on a page that non-admins can also open. */
  admin?: boolean;
}

/**
 * Individual settings, harvested from the labels of the settings panels. `admin` marks entries that live on
 * admin-only controls inside a page non-admins can open (the scrobbling page); whole admin-only pages are already
 * dropped by resolving each route against the same visibility rules as the navigator.
 */
const SETTINGS: readonly SettingDef[] = [
  // General
  { label: 'Application URL', section: 'general', elementId: 'general-application-url', keywords: 'base url public address host domain link invite external' },
  { label: 'Library manager', section: 'general', keywords: 'native lidarr mode switch backend manager' },
  // Media Management > Root Folders & Naming
  { label: 'Root Music Folder', section: 'media-management', leaf: 'media', keywords: 'library path directory music folder storage location' },
  { label: 'Download folders', section: 'media-management', leaf: 'media', anchor: 'Download folders', keywords: 'client roots download path read from clients' },
  { label: 'Extra import folder', section: 'media-management', leaf: 'media', anchor: 'Extra import folder', keywords: 'staging import path advanced' },
  { label: 'Import mode (torrents)', section: 'media-management', leaf: 'media', anchor: 'Import mode', keywords: 'hardlink copy move torrent import seeding' },
  { label: 'Tagging hardlinked torrent files', section: 'media-management', leaf: 'media', anchor: 'Tagging hardlinked', keywords: 'hardlink tags torrent write' },
  { label: 'When seeding is done', section: 'media-management', leaf: 'media', anchor: 'When seeding is done', keywords: 'seed cleanup remove torrent keep seeding delete after seeding' },
  { label: 'Write Tags', section: 'media-management', leaf: 'media', keywords: 'id3 metadata tagging audio tags' },
  { label: 'Embed Artwork', section: 'media-management', leaf: 'media', keywords: 'cover art album art image embed' },
  { label: 'Normalize Audio Tags', section: 'media-management', leaf: 'media', keywords: 'clean tags normalise' },
  { label: 'Monitoring: artists found by library scan', section: 'media-management', leaf: 'media', anchor: 'Artists found by library scan', keywords: 'monitor existing scan default new artists' },
  { label: 'Monitoring: artists added manually', section: 'media-management', leaf: 'media', anchor: 'Artists added manually', keywords: 'monitor default add artist' },
  { label: 'AcoustID API key', section: 'media-management', leaf: 'media', keywords: 'fingerprint audio fingerprinting musicbrainz identify key' },
  { label: 'Identify by fingerprint', section: 'media-management', leaf: 'media', anchor: 'Identify by fingerprint', keywords: 'acoustid fingerprint ambiguous tags' },
  { label: 'Artist Folder Format', section: 'media-management', leaf: 'media', keywords: 'naming rename template token folder' },
  { label: 'Standard Track Format', section: 'media-management', leaf: 'media', keywords: 'naming rename template token file name' },
  { label: 'Multi Disc Track Format', section: 'media-management', leaf: 'media', keywords: 'naming rename template disc cd' },
  { label: 'Recycle bin folder', section: 'media-management', leaf: 'media', keywords: 'trash deleted files recycle' },
  { label: 'Delete recycled files after (days)', section: 'media-management', leaf: 'media', anchor: 'Delete recycled files', keywords: 'recycle bin retention purge days' },
  { label: 'Delete replaced files permanently', section: 'media-management', leaf: 'media', anchor: 'Delete replaced files', keywords: 'recycle upgrade permanent delete' },
  { label: 'Quarantine folder', section: 'media-management', leaf: 'media', keywords: 'bad imports suspicious files security' },
  { label: 'Import iTunes / Apple Music library', section: 'media-management', leaf: 'media', anchor: 'Import iTunes', keywords: 'itunes apple music xml import library' },
  // Quality
  { label: 'Quality definitions', section: 'media-management', leaf: 'quality', anchor: 'Reset all qualities', keywords: 'quality sizes bitrate min max flac mp3 size limits' },
  // Profiles
  { label: 'Quality Profiles', section: 'media-management', leaf: 'profiles', keywords: 'quality profile cutoff upgrade allowed formats' },
  { label: 'Delay Profiles', section: 'media-management', leaf: 'profiles', keywords: 'delay wait usenet torrent preferred protocol' },
  { label: 'Metadata Profiles', section: 'media-management', leaf: 'profiles', keywords: 'album types primary secondary release statuses studio live ep single' },
  { label: 'Release Profiles', section: 'media-management', leaf: 'profiles', keywords: 'must contain must not contain preferred words ignored' },
  { label: 'Tags', section: 'media-management', leaf: 'profiles', anchor: 'Tags', keywords: 'tag labels assign profiles' },
  { label: 'Default for new artists', section: 'media-management', leaf: 'profiles', keywords: 'default metadata profile new artist' },
  { label: 'Release tester', section: 'media-management', leaf: 'profiles', anchor: 'Release tester', keywords: 'evaluate release title test scoring' },
  // Custom formats
  { label: 'Custom Formats', section: 'media-management', leaf: 'custom-formats', keywords: 'score specification regex release format import export' },
  // Clients
  { label: 'Add Download Client', section: 'media-management', leaf: 'clients', keywords: 'sabnzbd qbittorrent slskd soulseek usenet torrent nzb client host url' },
  { label: 'qBittorrent', section: 'media-management', leaf: 'clients', anchor: 'Add Download Client', keywords: 'torrent client' },
  { label: 'SABnzbd', section: 'media-management', leaf: 'clients', anchor: 'Add Download Client', keywords: 'usenet nzb client' },
  { label: 'slskd', section: 'media-management', leaf: 'clients', anchor: 'Add Download Client', keywords: 'soulseek client' },
  // Indexers
  { label: 'Indexer API Key', section: 'media-management', leaf: 'indexers', anchor: 'API Key', keywords: 'newznab torznab prowlarr jackett indexer key' },
  { label: 'Seed ratio', section: 'media-management', leaf: 'indexers', keywords: 'seed rules seeding ratio torrent share' },
  { label: 'Minimum seeders', section: 'media-management', leaf: 'indexers', keywords: 'seed rules seeders torrent' },
  { label: 'Seed time (minutes)', section: 'media-management', leaf: 'indexers', anchor: 'Seed time', keywords: 'seed rules seeding duration torrent' },
  { label: 'Discography seed time (minutes)', section: 'media-management', leaf: 'indexers', anchor: 'Discography seed time', keywords: 'seed rules seeding discography' },
  // Import lists
  { label: 'Add import list', section: 'media-management', leaf: 'import-lists', anchor: 'Add list', keywords: 'import list spotify lastfm listenbrainz playlist sync auto add artists' },
  // Media server
  { label: 'Media Server', section: 'media-management', leaf: 'media-server', anchor: 'Media Server', keywords: 'plex jellyfin navidrome subsonic emby server library refresh' },
  // Lidarr
  { label: 'Lidarr Host URL', section: 'lidarr', elementId: 'lidarr-lidarr-host-url', keywords: 'lidarr server address url connection' },
  { label: 'Lidarr API Key', section: 'lidarr', elementId: 'lidarr-lidarr-api-key', keywords: 'lidarr key token secret' },
  { label: 'Lidarr Root Folder', section: 'lidarr', elementId: 'lidarr-root-folder', keywords: 'lidarr path music folder' },
  { label: 'Search on Add', section: 'lidarr', keywords: 'lidarr automatic search new artist' },
  { label: 'Prefer singles for song requests', section: 'lidarr', keywords: 'track request single lidarr' },
  { label: 'Auto Trickle', section: 'lidarr', keywords: 'trickle throttle rate limit batch searches' },
  { label: 'Trickle Rate (Seconds)', section: 'lidarr', elementId: 'lidarr-trickle-rate-seconds', keywords: 'auto trickle interval' },
  { label: 'Trickle Batch Size', section: 'lidarr', elementId: 'lidarr-trickle-batch-size', keywords: 'auto trickle batch' },
  // Requests > Users
  { label: 'Account defaults', section: 'requests', leaf: 'users', keywords: 'default new users permissions' },
  { label: 'Quota defaults: tracks per window', section: 'requests', leaf: 'users', anchor: 'Tracks per window', keywords: 'request quota limit default tracks' },
  { label: 'Quota defaults: albums per window', section: 'requests', leaf: 'users', anchor: 'Albums per window', keywords: 'request quota limit default albums' },
  { label: 'Quota defaults: discographies per window', section: 'requests', leaf: 'users', anchor: 'Discographies per window', keywords: 'request quota limit default discography' },
  { label: 'Quota window (days)', section: 'requests', leaf: 'users', anchor: 'Window (days)', keywords: 'request quota period rolling days' },
  { label: 'Require MFA for local accounts', section: 'requests', leaf: 'users', keywords: 'two factor 2fa security authentication mandatory' },
  // Requests > Scrobbling
  { label: 'Music Identity', section: 'requests', leaf: 'scrobbling', keywords: 'listens profile lastfm listenbrainz scrobble link account' },
  { label: 'ListenBrainz user token', section: 'requests', leaf: 'scrobbling', keywords: 'listenbrainz token scrobble' },
  { label: 'Scrobbling enabled', section: 'requests', leaf: 'scrobbling', keywords: 'scrobble toggle listens' },
  { label: 'Server Scrobbling', section: 'requests', leaf: 'scrobbling', admin: true, keywords: 'lastfm server scrobbling admin' },
  { label: 'Last.fm API Key', section: 'requests', leaf: 'scrobbling', admin: true, keywords: 'lastfm last fm key api scrobble' },
  { label: 'Last.fm API Secret', section: 'requests', leaf: 'scrobbling', admin: true, anchor: 'Last.fm API Secret', keywords: 'lastfm last fm secret' },
  { label: 'Plex Webhook', section: 'requests', leaf: 'scrobbling', admin: true, keywords: 'webhook plex playback scrobble url' },
  // Requests > Notifications
  { label: 'Notification channels', section: 'requests', leaf: 'notifications', anchor: 'Add channel', keywords: 'alerts notify events add channel' },
  { label: 'Discord', section: 'requests', leaf: 'notifications', anchor: 'Add channel', keywords: 'discord webhook notification channel' },
  { label: 'Telegram', section: 'requests', leaf: 'notifications', anchor: 'Add channel', keywords: 'telegram bot notification channel' },
  { label: 'Pushover', section: 'requests', leaf: 'notifications', anchor: 'Add channel', keywords: 'pushover push notification channel' },
  { label: 'Webhook', section: 'requests', leaf: 'notifications', anchor: 'Add channel', keywords: 'http webhook notification channel generic' },
  { label: 'Email notifications', section: 'requests', leaf: 'notifications', anchor: 'Add channel', keywords: 'smtp mail notification channel' },
  // System
  { label: 'System Diagnostics', section: 'system', leaf: 'status', keywords: 'version uptime database health workers' },
  { label: 'Lidarr Health', section: 'system', leaf: 'status', keywords: 'lidarr connection health status' },
  { label: 'Tasks & Background Workers', section: 'system', leaf: 'tasks', anchor: 'Scheduled Tasks', keywords: 'cron run now cancel interval scan sync schedule history job queue events resources cpu memory' },
  { label: 'Application Logs', section: 'system', leaf: 'logs', keywords: 'log stream debug console output' },
  // Account
  { label: 'Change password', section: 'account', keywords: 'password credentials security' },
  { label: 'Two-factor authentication', section: 'account', keywords: 'mfa 2fa totp authenticator recovery codes security' },
  { label: 'Request quota', section: 'account', keywords: 'limit usage remaining requests' },
];

const pageLabel = (route: AppRoute, tree: readonly NavNode[], trail: string[] = []): string[] | null => {
  for (const node of tree) {
    const here = [...trail, node.label];
    if (node.route && routesEqual(node.route, route)) return here;
    if (node.children) {
      const found = pageLabel(route, node.children, here);
      if (found) return found;
    }
  }
  return null;
};

const leavesOf = (nodes: readonly NavNode[], trail: string[] = []): Array<{ node: NavNode; path: string[] }> =>
  nodes.flatMap((node) => {
    const path = [...trail, node.label];
    return node.children ? leavesOf(node.children, path) : node.route ? [{ node, path }] : [];
  });

const DESCRIPTION_WORDS: Record<string, string> = {
  discover: 'search explore trending browse catalog find music',
  requests: 'request queue approvals pending',
  library: 'collection synced albums artists',
  playlists: 'spotify tidal sync lists',
  activity: 'downloads queue lidarr',
  wanted: 'missing cutoff upgrade',
};

/**
 * Every destination this user can reach, built from the same tree the hub renders (so visibility rules are shared),
 * plus the individual settings that live inside visible settings pages.
 */
export function buildNavSearchIndex(access: RouteAccess): NavSearchEntry[] {
  const tree = buildNavTree(access);
  const entries: NavSearchEntry[] = [];

  for (const { node, path } of leavesOf(tree)) {
    if (!node.route) continue;
    const root = path[0].toLowerCase();
    entries.push({
      key: `page:${node.key}`,
      label: node.label,
      breadcrumb: path,
      route: node.route,
      keywords: [DESCRIPTION_WORDS[root] ?? '', node.description ?? ''].filter((w) => w.length > 0),
    });
  }

  for (const def of SETTINGS) {
    if (def.admin && !access.isAdmin) continue;
    const wanted = settingsRouteFor(def.section, def.leaf);
    const visible = resolveSettingsRoute(wanted, access.isAdmin, access.mfaEnrollmentRequired);
    // Hidden page for this user: the resolver falls back to another page, so the route no longer matches.
    if (!routesEqual(visible, wanted)) continue;
    const crumbs = pageLabel(wanted, tree);
    if (!crumbs) continue;
    entries.push({
      key: `setting:${def.section}/${def.leaf ?? ''}/${def.label}`,
      label: def.label,
      breadcrumb: [...crumbs, def.label],
      route: wanted,
      keywords: def.keywords.split(' '),
      target: { elementId: def.elementId, anchor: def.anchor ?? def.label },
    });
  }
  return entries;
}
