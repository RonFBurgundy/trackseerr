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
  { label: 'API Key', section: 'general', elementId: 'general-api-key', keywords: 'api key token regenerate secret auth credentials external integration' },
  // Media Management > Root Folders & Naming
  { label: 'Root Folders & Naming', section: 'media-management', leaf: 'media', anchor: 'Root Folders & Naming', keywords: 'root folders paths library naming audio tagging templates' },
  { label: 'Root Music Folder', section: 'media-management', leaf: 'media', keywords: 'library path directory music folder storage location' },
  { label: 'Download folders', section: 'media-management', leaf: 'media', anchor: 'Download folders', keywords: 'client roots download path read from clients' },
  { label: 'Extra import folder', section: 'media-management', leaf: 'media', anchor: 'Extra import folder', keywords: 'staging import path advanced' },
  { label: 'Import mode (torrents)', section: 'media-management', leaf: 'media', anchor: 'Import mode', keywords: 'hardlink copy move torrent import seeding' },
  { label: 'Tagging hardlinked torrent files', section: 'media-management', leaf: 'media', anchor: 'Tagging hardlinked', keywords: 'hardlink tags torrent write' },
  { label: 'When seeding is done', section: 'media-management', leaf: 'media', anchor: 'When seeding is done', keywords: 'seed cleanup remove torrent keep seeding delete after seeding' },
  { label: 'Write Tags', section: 'media-management', leaf: 'media', keywords: 'id3 metadata tagging audio tags' },
  { label: 'Embed Artwork', section: 'media-management', leaf: 'media', keywords: 'cover art album art image embed' },
  { label: 'Normalize Audio Tags', section: 'media-management', leaf: 'media', keywords: 'clean tags normalise' },
  { label: 'Monitoring', section: 'media-management', leaf: 'media', anchor: 'Monitoring', keywords: 'monitoring defaults new artists library scan manual existing' },
  { label: 'Monitoring: artists found by library scan', section: 'media-management', leaf: 'media', anchor: 'Artists found by library scan', keywords: 'monitor existing scan default new artists' },
  { label: 'Monitoring: artists added manually', section: 'media-management', leaf: 'media', anchor: 'Artists added manually', keywords: 'monitor default add artist' },
  { label: 'Keep the same song from more than one release', section: 'media-management', leaf: 'media', anchor: 'Keep the same song from more than one release', keywords: 'duplicate tracks editions allow multiple releases same song' },
  { label: 'Audio fingerprinting', section: 'media-management', leaf: 'media', anchor: 'Audio fingerprinting', keywords: 'audio fingerprinting acoustid identify mbids sound match' },
  { label: 'AcoustID API key', section: 'media-management', leaf: 'media', keywords: 'fingerprint audio fingerprinting musicbrainz identify key' },
  { label: 'AcoustID user key', section: 'media-management', leaf: 'media', anchor: 'AcoustID user key', keywords: 'acoustid user key submit contributions mbids account' },
  { label: 'Identify by fingerprint', section: 'media-management', leaf: 'media', anchor: 'Identify by fingerprint', keywords: 'acoustid fingerprint ambiguous tags' },
  { label: 'Artist Folder Format', section: 'media-management', leaf: 'media', keywords: 'naming rename template token folder' },
  { label: 'Standard Track Format', section: 'media-management', leaf: 'media', keywords: 'naming rename template token file name' },
  { label: 'Multi Disc Track Format', section: 'media-management', leaf: 'media', keywords: 'naming rename template disc cd' },
  { label: 'Compilation File Name', section: 'media-management', leaf: 'media', anchor: 'Compilation File Name', keywords: 'compilation file name track various artists format va' },
  { label: 'Compilation artist folder', section: 'media-management', leaf: 'media', anchor: 'Compilation artist folder', keywords: 'compilation artist folder various artists va compilations directory' },
  { label: 'Remix credit', section: 'media-management', leaf: 'media', anchor: 'Remix credit', keywords: 'remix credit original remixer as credited musicbrainz file name tags' },
  { label: 'Featured artists', section: 'media-management', leaf: 'media', anchor: 'Featured artists', keywords: 'featured artists feat credit in title in artist tags' },
  { label: 'Replace illegal characters', section: 'media-management', leaf: 'media', anchor: 'Replace illegal characters', keywords: 'replace illegal characters filenames sanitize windows paths' },
  { label: 'Rename artist folders', section: 'media-management', leaf: 'media', anchor: 'Rename artist folders', keywords: 'rename artist folders tidy album directory structure' },
  { label: 'Recycle bin & quarantine', section: 'media-management', leaf: 'media', anchor: 'Recycle bin & quarantine', keywords: 'recycle bin quarantine deleted replaced files trash' },
  { label: 'Recycle bin folder', section: 'media-management', leaf: 'media', keywords: 'trash deleted files recycle' },
  { label: 'Delete recycled files after (days)', section: 'media-management', leaf: 'media', anchor: 'Delete recycled files', keywords: 'recycle bin retention purge days' },
  { label: 'Empty recycle bin', section: 'media-management', leaf: 'media', anchor: 'Empty recycle bin', keywords: 'empty recycle bin purge delete permanently clean trash' },
  { label: 'Delete replaced files permanently', section: 'media-management', leaf: 'media', anchor: 'Delete replaced files', keywords: 'recycle upgrade permanent delete' },
  { label: 'Quarantine folder', section: 'media-management', leaf: 'media', keywords: 'bad imports suspicious files security' },
  // Quality
  { label: 'Quality definitions', section: 'media-management', leaf: 'quality', anchor: 'Reset all', keywords: 'quality sizes bitrate min max flac mp3 size limits' },
  { label: "Check each track's bitrate on import", section: 'media-management', leaf: 'quality', anchor: "Check each track's bitrate on import", keywords: 'check bitrate on import warn reject fake flac verification quality' },
  // Profiles
  { label: 'Quality Profiles', section: 'media-management', leaf: 'profiles', keywords: 'quality profile cutoff upgrade allowed formats' },
  { label: 'Delay Profiles', section: 'media-management', leaf: 'profiles', keywords: 'delay wait usenet torrent preferred protocol' },
  { label: 'Pending releases', section: 'media-management', leaf: 'profiles', anchor: 'Pending releases', keywords: 'pending releases delay held queue grab now' },
  { label: 'Metadata Profiles', section: 'media-management', leaf: 'profiles', keywords: 'album types primary secondary release statuses studio live ep single' },
  { label: 'Default for new artists', section: 'media-management', leaf: 'profiles', keywords: 'default metadata profile new artist' },
  { label: 'Release Profiles', section: 'media-management', leaf: 'profiles', keywords: 'must contain must not contain preferred words ignored' },
  { label: 'Tags', section: 'media-management', leaf: 'profiles', anchor: 'Tags', keywords: 'tag labels assign profiles' },
  { label: 'Release tester', section: 'media-management', leaf: 'profiles', anchor: 'Quality Profiles', keywords: 'evaluate release title test scoring' },
  // Custom formats
  { label: 'Custom Formats', section: 'media-management', leaf: 'custom-formats', elementId: 'custom-formats-overview', keywords: 'score specification regex release format import export' },
  { label: 'Import custom format', section: 'media-management', leaf: 'custom-formats', anchor: 'Import', keywords: 'import custom format json lidarr servarr file upload' },
  { label: 'Add custom format', section: 'media-management', leaf: 'custom-formats', anchor: 'Add', keywords: 'add new custom format create rule score' },
  // Clients
  { label: 'Add Download Client', section: 'media-management', leaf: 'clients', keywords: 'sabnzbd qbittorrent slskd soulseek usenet torrent nzb transmission deluge nzbget client host url' },
  { label: 'Download Client Host URL', section: 'media-management', leaf: 'clients', anchor: 'Host URL', keywords: 'download client host url address port connection' },
  { label: 'Soulseek tuning', section: 'media-management', leaf: 'clients', anchor: 'Soulseek tuning', keywords: 'soulseek slskd tuning search timeout slots upload speed ignored users' },
  { label: 'qBittorrent', section: 'media-management', leaf: 'clients', anchor: 'Add Download Client', keywords: 'torrent client' },
  { label: 'Transmission', section: 'media-management', leaf: 'clients', anchor: 'Add Download Client', keywords: 'torrent client transmission' },
  { label: 'Deluge', section: 'media-management', leaf: 'clients', anchor: 'Add Download Client', keywords: 'torrent client deluge' },
  { label: 'SABnzbd', section: 'media-management', leaf: 'clients', anchor: 'Add Download Client', keywords: 'usenet nzb client' },
  { label: 'NZBGet', section: 'media-management', leaf: 'clients', anchor: 'Add Download Client', keywords: 'usenet nzb client nzbget' },
  { label: 'slskd', section: 'media-management', leaf: 'clients', anchor: 'Add Download Client', keywords: 'soulseek client' },
  // Indexers
  { label: 'Add New Indexer', section: 'media-management', leaf: 'indexers', anchor: 'Add New Indexer', keywords: 'torznab newznab prowlarr jackett add new indexer torrent usenet search' },
  { label: 'Indexer API Key', section: 'media-management', leaf: 'indexers', anchor: 'API Key', keywords: 'newznab torznab prowlarr jackett indexer key' },
  { label: 'Indexer Seeding', section: 'media-management', leaf: 'indexers', anchor: 'Seeding', keywords: 'seeding rules ratio seed time discography minimum seeders torrent' },
  { label: 'Seed ratio', section: 'media-management', leaf: 'indexers', keywords: 'seed rules seeding ratio torrent share' },
  { label: 'Minimum seeders', section: 'media-management', leaf: 'indexers', keywords: 'seed rules seeders torrent' },
  { label: 'Seed time (minutes)', section: 'media-management', leaf: 'indexers', anchor: 'Seed time', keywords: 'seed rules seeding duration torrent' },
  { label: 'Discography seed time (minutes)', section: 'media-management', leaf: 'indexers', anchor: 'Discography seed time', keywords: 'seed rules seeding discography' },
  // Import lists
  { label: 'Add import list', section: 'media-management', leaf: 'import-lists', anchor: 'Add list', keywords: 'import list spotify lastfm listenbrainz playlist sync auto add artists' },
  { label: 'Import iTunes / Apple Music library', section: 'media-management', leaf: 'import-lists', anchor: 'Import iTunes', keywords: 'itunes apple music xml import library playlist tracks' },
  // Media server
  { label: 'Media Server', section: 'media-management', leaf: 'media-server', anchor: 'Media Server', keywords: 'plex jellyfin navidrome subsonic emby server library refresh' },
  { label: 'Media Server Type', section: 'media-management', leaf: 'media-server', anchor: 'Server type', keywords: 'media server type plex jellyfin subsonic navidrome connection integration' },
  { label: 'Media Server URL', section: 'media-management', leaf: 'media-server', anchor: 'Server URL', keywords: 'media server host url address http jellyfin navidrome' },
  // Lidarr
  { label: 'Lidarr Integration', section: 'lidarr', anchor: 'Lidarr Integration', keywords: 'lidarr server connection integration settings host url' },
  { label: 'Lidarr Host URL', section: 'lidarr', elementId: 'lidarr-lidarr-host-url', keywords: 'lidarr server address url connection' },
  { label: 'Lidarr API Key', section: 'lidarr', elementId: 'lidarr-lidarr-api-key', keywords: 'lidarr key token secret' },
  { label: 'Lidarr Root Folder', section: 'lidarr', elementId: 'lidarr-root-folder', keywords: 'lidarr path music folder' },
  { label: 'Search on Add', section: 'lidarr', keywords: 'lidarr automatic search new artist' },
  { label: 'Prefer singles for song requests', section: 'lidarr', keywords: 'track request single lidarr' },
  { label: 'Auto Trickle', section: 'lidarr', keywords: 'trickle throttle rate limit batch searches' },
  { label: 'Trickle Rate (Seconds)', section: 'lidarr', elementId: 'lidarr-trickle-rate-seconds', keywords: 'auto trickle interval' },
  { label: 'Trickle Batch Size', section: 'lidarr', elementId: 'lidarr-trickle-batch-size', keywords: 'auto trickle batch' },
  // Requests > Users
  { label: 'Users list', section: 'requests', leaf: 'users', anchor: 'Users', keywords: 'users list accounts members permissions roles manage' },
  { label: 'Create local user', section: 'requests', leaf: 'users', anchor: 'Create local user', keywords: 'create local user add new account registration password' },
  { label: 'Account defaults', section: 'requests', leaf: 'users', keywords: 'default new users permissions' },
  { label: 'Quota defaults: tracks per window', section: 'requests', leaf: 'users', anchor: 'Tracks per window', keywords: 'request quota limit default tracks' },
  { label: 'Quota defaults: albums per window', section: 'requests', leaf: 'users', anchor: 'Albums per window', keywords: 'request quota limit default albums' },
  { label: 'Quota defaults: discographies per window', section: 'requests', leaf: 'users', anchor: 'Discographies per window', keywords: 'request quota limit default discography' },
  { label: 'Quota window (days)', section: 'requests', leaf: 'users', anchor: 'Window (days)', keywords: 'request quota period rolling days' },
  { label: 'Require MFA for local accounts', section: 'requests', leaf: 'users', keywords: 'two factor 2fa security authentication mandatory' },
  // Requests > Scrobbling
  { label: 'Music Identity', section: 'requests', leaf: 'scrobbling', keywords: 'listens profile lastfm listenbrainz scrobble link account' },
  { label: 'ListenBrainz user token', section: 'requests', leaf: 'scrobbling', anchor: 'ListenBrainz', keywords: 'listenbrainz token scrobble' },
  { label: 'Scrobbling enabled', section: 'requests', leaf: 'scrobbling', keywords: 'scrobble toggle listens' },
  { label: 'Recent Listens', section: 'requests', leaf: 'scrobbling', anchor: 'Recent Listens', keywords: 'recent listens history tracks scrobbled played songs' },
  { label: 'Server Scrobbling', section: 'requests', leaf: 'scrobbling', admin: true, keywords: 'lastfm server scrobbling admin' },
  { label: 'Last.fm API Key', section: 'requests', leaf: 'scrobbling', admin: true, keywords: 'lastfm last fm key api scrobble' },
  { label: 'Last.fm API Secret', section: 'requests', leaf: 'scrobbling', admin: true, anchor: 'Last.fm API Secret', keywords: 'lastfm last fm secret' },
  { label: 'Plex History Poll', section: 'requests', leaf: 'scrobbling', admin: true, anchor: 'Plex History Poll', keywords: 'plex history poll interval minutes scrobble server sync' },
  { label: 'Plex Webhook', section: 'requests', leaf: 'scrobbling', admin: true, keywords: 'webhook plex playback scrobble url' },
  { label: 'Scrobbling Users', section: 'requests', leaf: 'scrobbling', admin: true, anchor: 'Users', keywords: 'scrobbling users linked accounts status plex lastfm listenbrainz' },
  // Requests > Notifications
  { label: 'Notification channels', section: 'requests', leaf: 'notifications', anchor: 'Add channel', keywords: 'alerts notify events add channel' },
  { label: 'Discord', section: 'requests', leaf: 'notifications', anchor: 'Add channel', keywords: 'discord webhook notification channel' },
  { label: 'Telegram', section: 'requests', leaf: 'notifications', anchor: 'Add channel', keywords: 'telegram bot notification channel' },
  { label: 'Pushover', section: 'requests', leaf: 'notifications', anchor: 'Add channel', keywords: 'pushover push notification channel' },
  { label: 'Webhook', section: 'requests', leaf: 'notifications', anchor: 'Add channel', keywords: 'http webhook notification channel generic' },
  { label: 'Email notifications', section: 'requests', leaf: 'notifications', anchor: 'Add channel', keywords: 'smtp mail notification channel' },
  // System
  { label: 'System Diagnostics', section: 'system', leaf: 'status', keywords: 'version uptime database health workers' },
  { label: 'System Health', section: 'system', leaf: 'status', anchor: 'Health', keywords: 'health status checks disk space download client media server alerts problems failures' },
  { label: 'Health Alert Thresholds', section: 'system', leaf: 'status', anchor: 'Alert thresholds', keywords: 'health alert thresholds disk space free gb stall minutes fail timeout' },
  { label: 'Software Updates', section: 'system', leaf: 'status', anchor: 'Software Updates', keywords: 'software updates check new version github releases update available' },
  { label: 'Automatic Update Checks', section: 'system', leaf: 'status', anchor: 'Automatic Checks', keywords: 'automatic update checks interval 12h schedule background' },
  { label: 'All Releases / Changelog', section: 'system', leaf: 'status', anchor: 'All releases', keywords: 'changelog release notes history versions updates what is new' },
  { label: "What's New", section: 'system', leaf: 'status', anchor: "What's new", keywords: 'whats new changelog latest release features bug fixes notes' },
  { label: 'Lidarr Health', section: 'system', leaf: 'status', keywords: 'lidarr connection health status' },
  { label: 'Tasks & Background Workers', section: 'system', leaf: 'tasks', anchor: 'Scheduled Tasks', keywords: 'cron run now cancel interval scan sync schedule history job queue events resources cpu memory' },
  { label: 'System Resources', section: 'system', leaf: 'tasks', elementId: 'task-resources-strip', keywords: 'cpu memory rss threads uptime system process resources gauge' },
  { label: 'Database Backups & Restore', section: 'system', leaf: 'backups', keywords: 'backup restore database snapshot download upload retention' },
  { label: 'Back up now', section: 'system', leaf: 'backups', anchor: 'Back up now', keywords: 'create database backup snapshot manual backup now' },
  { label: 'Restore backup from file', section: 'system', leaf: 'backups', anchor: 'Restore from file', keywords: 'restore database backup upload file archive snapshot' },
  { label: 'Backup Settings', section: 'system', leaf: 'backups', anchor: 'Settings', keywords: 'backup settings retention scheduled interval frequency automated' },
  { label: 'Application Logs', section: 'system', leaf: 'logs', keywords: 'log stream debug console output' },
  { label: 'Log Settings', section: 'system', leaf: 'logs', anchor: 'Log Settings', keywords: 'log settings level rotation retention days max size mb debug trace info' },
  { label: 'Download Log', section: 'system', leaf: 'logs', anchor: 'Download Log', keywords: 'download log files export save text' },
  { label: 'Clear Logs', section: 'system', leaf: 'logs', anchor: 'Clear Logs', keywords: 'clear logs truncate delete purge output' },
  // Account
  { label: 'Account details', section: 'account', anchor: 'Account', keywords: 'account username profile auth sign in plex local' },
  { label: 'Request quota', section: 'account', keywords: 'limit usage remaining requests' },
  { label: 'Change password', section: 'account', keywords: 'password credentials security' },
  { label: 'Two-factor authentication', section: 'account', keywords: 'mfa 2fa totp authenticator recovery codes security' },
  { label: 'Browser Web Push', section: 'account', anchor: 'Browser Web Push', keywords: 'browser web push notifications device alerts enable subscribe' },
  { label: 'Delivery Preferences', section: 'account', anchor: 'Delivery Preferences', keywords: 'delivery preferences notifications inbox push events alerts preferences' },
  { label: 'Server alerts (admins)', section: 'account', admin: true, anchor: 'Server alerts (admins)', keywords: 'server alerts admin notifications system errors warnings health' },
  { label: 'Personal Channels', section: 'account', anchor: 'Personal Channels', keywords: 'personal notification channels discord telegram pushover webhook user alerts' },
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
  calendar: 'releases schedule upcoming albums ical feed',
};

const LEAF_WORDS: Record<string, string> = {
  'requests/all': 'all requests queue browse filter',
  'requests/pending': 'pending requests approval review unapproved',
  'requests/approved': 'approved requests processing queued downloading',
  'requests/fulfilled': 'fulfilled completed available in library finished',
  'requests/rejected': 'rejected declined denied requests',
  'requests/issues': 'problems report wrong file recycle quarantine audio issues missing tracks',
  'library/artists': 'artists discography library musicians singers bands',
  'library/albums': 'albums releases discography record lp ep',
  'library/tracks': 'tracks songs audio files singles music list',
  'library/collections': 'collections boxed sets custom lists series anthologies',
  'activity/queue': 'downloads queue active progress transfer clients speed',
  'activity/history': 'download history grabs imported completed failed activity log',
  'activity/blocklist': 'blocklist blacklisted releases ignored failed bad releases block',
  'activity/searches': 'search log soulseek indexer results zero responses',
  'activity/review': 'needs review manual import held files soulseek chat challenge',
  'wanted/missing': 'missing wanted albums tracks search missing unmonitored',
  'wanted/cutoff': 'cutoff unmet quality upgrade better quality wanted',
  'wanted/calendar': 'calendar releases schedule upcoming albums ical feed',
  'settings/general': 'application url host base api key library manager mode',
  'settings/media-management/media': 'root music folder download staging naming tags id3 acoustid fingerprint recycle quarantine',
  'settings/media-management/quality': 'quality definitions limits bitrate kbps flac mp3 size bounds',
  'settings/media-management/profiles': 'quality profiles delay profiles metadata profiles release profiles tags default',
  'settings/media-management/custom-formats': 'custom formats scores regex specifications servarr import export',
  'settings/media-management/clients': 'download clients torrent usenet soulseek qbittorrent transmission deluge sabnzbd nzbget slskd',
  'settings/media-management/indexers': 'indexers torznab newznab prowlarr jackett seeding seed ratio seed time',
  'settings/media-management/import-lists': 'import lists spotify lastfm listenbrainz itunes apple music sync automated',
  'settings/media-management/media-server': 'media server plex jellyfin subsonic navidrome connection sync',
  'settings/lidarr': 'lidarr integration url api key trickle auto trickle root folder',
  'settings/requests/users': 'users accounts permissions quotas defaults mfa local user',
  'settings/requests/scrobbling': 'scrobbling lastfm listenbrainz music identity plex webhook recent listens',
  'settings/requests/notifications': 'notification channels discord telegram pushover webhook email alerts',
  'settings/system/status': 'system diagnostics health status version update changelog lidarr health',
  'settings/system/tasks': 'scheduled tasks background workers cron queue resources cpu memory',
  'settings/system/backups': 'database backups restore snapshot retention upload schedule atomic',
  'settings/system/logs': 'application logs live stream log level debug rotation download clear',
  'settings/account': 'account password change two factor 2fa quota web push delivery preferences personal channels',
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
      keywords: [
        DESCRIPTION_WORDS[root] ?? '',
        LEAF_WORDS[node.key] ?? '',
        node.description ?? '',
      ].filter((w) => w.length > 0),
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
