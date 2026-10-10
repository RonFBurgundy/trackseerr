import { useCallback, useEffect, useState } from 'react';
import type { WantedListName } from '@/types/activity';
import type { LibraryTab } from './useLibrary';
import type { RequestFilter } from './useRequests';
import { parseImportHash, storePendingImport, peekPendingImport } from '@/services/bookmarkletImport';

export type MainTab = 'discover' | 'requests' | 'library' | 'playlists' | 'activity' | 'wanted' | 'settings';

export type RequestsSub = RequestFilter | 'issues';
export type ActivitySub = 'queue' | 'history' | 'blocklist' | 'review';
export type WantedSub = WantedListName | 'calendar';

export type SettingsSection = 'general' | 'media-management' | 'lidarr' | 'requests' | 'system' | 'account';
export type MediaManagementLeaf = 'media' | 'quality' | 'profiles' | 'custom-formats' | 'clients' | 'indexers' | 'import-lists' | 'media-server';
export type RequestsLeaf = 'users' | 'scrobbling' | 'notifications';
export type SystemLeaf = 'status' | 'tasks' | 'backups' | 'logs';
export type SettingsLeafId = MediaManagementLeaf | RequestsLeaf | SystemLeaf;

export type SettingsRoute =
  | { tab: 'settings'; sub: 'general' | 'lidarr' | 'account' }
  | { tab: 'settings'; sub: 'media-management'; leaf: MediaManagementLeaf }
  | { tab: 'settings'; sub: 'requests'; leaf: RequestsLeaf }
  | { tab: 'settings'; sub: 'system'; leaf: SystemLeaf };

/** Library drill-down carried in the URL: an artist or a collection, optionally with an album modal on top. */
export interface LibraryDetail {
  artistId?: string;
  collectionId?: string;
  albumId?: string;
}

/** Discover, optionally drilled into one artist profile (`#/discover/artist/<encodedDiscoveryId>`). */
export interface DiscoverRoute {
  tab: 'discover';
  artistId?: string;
}

/** Requests sub-page; `issueId` opens one issue on `#/requests/issues/<id>`. */
export interface RequestsRoute {
  tab: 'requests';
  sub: RequestsSub;
  issueId?: string;
}

/** Activity sub-page. */
export interface ActivityRoute {
  tab: 'activity';
  sub: ActivitySub;
}

export interface LibraryRoute {
  tab: 'library';
  sub: LibraryTab;
  detail?: LibraryDetail;
}

/** Current location. `sub` is always present for tabs that have sub-page; `leaf` only under settings sections with children. */
export type AppRoute =
  | DiscoverRoute
  | { tab: 'playlists' }
  | RequestsRoute
  | LibraryRoute
  | ActivityRoute
  | { tab: 'wanted'; sub: WantedSub }
  | SettingsRoute;

export interface NavigateOptions {
  /** Replace the current history entry instead of pushing a new one. */
  replace?: boolean;
}

export type Navigate = (route: AppRoute, options?: NavigateOptions) => void;

/** History-state key holding the hash of the in-app entry we pushed from, so "back to parent" can use real history. */
const PREV_KEY = '__tsPrevHash';

function prevHashOf(state: unknown): string | undefined {
  if (typeof state === 'object' && state !== null && PREV_KEY in state) {
    const value = state[PREV_KEY];
    return typeof value === 'string' ? value : undefined;
  }
  return undefined;
}

export const MAIN_TABS: readonly MainTab[] = ['discover', 'requests', 'library', 'playlists', 'activity', 'wanted', 'settings'];
export const REQUESTS_SUBS: readonly RequestsSub[] = ['all', 'pending', 'approved', 'fulfilled', 'rejected', 'issues'];
export const LIBRARY_SUBS: readonly LibraryTab[] = ['artists', 'albums', 'tracks', 'collections'];
export const ACTIVITY_SUBS: readonly ActivitySub[] = ['queue', 'history', 'blocklist', 'review'];
export const WANTED_SUBS: readonly WantedSub[] = ['missing', 'cutoff', 'calendar'];
export const SETTINGS_SECTIONS: readonly SettingsSection[] = [
  'general',
  'media-management',
  'lidarr',
  'requests',
  'system',
  'account',
];
export const MEDIA_MANAGEMENT_LEAVES: readonly MediaManagementLeaf[] = [
  'media',
  'quality',
  'profiles',
  'custom-formats',
  'clients',
  'indexers',
  'import-lists',
  'media-server',
];
export const REQUESTS_LEAVES: readonly RequestsLeaf[] = ['users', 'scrobbling', 'notifications'];
export const SYSTEM_LEAVES: readonly SystemLeaf[] = ['status', 'tasks', 'backups', 'logs'];
/** Retired System leaves folded into the Tasks page; old bookmarks land there instead of on Status. */
const LEGACY_SYSTEM_LEAVES: readonly string[] = ['queue', 'events'];

/** Narrow an untrusted string to a member of `list` without a cast. */
export function pick<T extends string>(list: readonly T[], value: string | null | undefined): T | undefined {
  return list.find((candidate) => candidate === value);
}

/** Build a valid settings route; an unknown or missing leaf falls back to the section's first leaf. */
export function settingsRouteFor(section: SettingsSection, leaf?: string | null): SettingsRoute {
  switch (section) {
    case 'media-management':
      return { tab: 'settings', sub: section, leaf: pick(MEDIA_MANAGEMENT_LEAVES, leaf) ?? 'media' };
    case 'requests':
      return { tab: 'settings', sub: section, leaf: pick(REQUESTS_LEAVES, leaf) ?? 'users' };
    case 'system':
      return { tab: 'settings', sub: section, leaf: pick(SYSTEM_LEAVES, leaf) ?? (LEGACY_SYSTEM_LEAVES.includes(leaf ?? '') ? 'tasks' : 'status') };
    default:
      return { tab: 'settings', sub: section };
  }
}

/** The landing route of a main tab. */
export function defaultRoute(tab: MainTab): AppRoute {
  switch (tab) {
    case 'requests':
      return { tab, sub: 'all' };
    case 'library':
      return { tab, sub: 'artists' };
    case 'activity':
      return { tab, sub: 'queue' };
    case 'wanted':
      return { tab, sub: 'missing' };
    case 'settings':
      return settingsRouteFor('general');
    default:
      return { tab };
  }
}

function libraryDetailSegments(detail: LibraryDetail | undefined): string[] {
  if (!detail) return [];
  const parts: string[] = [];
  // An artist wins over a collection; the two never coexist in one URL.
  if (detail.artistId) parts.push('artist', encodeURIComponent(detail.artistId));
  else if (detail.collectionId) parts.push('collection', encodeURIComponent(detail.collectionId));
  if (detail.albumId) parts.push('album', encodeURIComponent(detail.albumId));
  return parts;
}

/** `#/<tab>[/<sub>[/<leaf>]]`; discover adds `/artist/<id>`, library adds `/artist/<id>`, `/collection/<id>` and `/album/<id>` drill-down segments. */
export function routeToHash(route: AppRoute): string {
  const parts: string[] = [route.tab];
  if ('sub' in route) parts.push(route.sub);
  if ('leaf' in route) parts.push(route.leaf);
  if (route.tab === 'library') parts.push(...libraryDetailSegments(route.detail));
  if (route.tab === 'requests' && route.sub === 'issues' && route.issueId) parts.push(encodeURIComponent(route.issueId));
  if (route.tab === 'discover' && route.artistId) parts.push('artist', encodeURIComponent(route.artistId));
  return `#/${parts.join('/')}`;
}

function decodeSegment(raw: string | undefined): string | undefined {
  if (raw === undefined) return undefined;
  try {
    const value = decodeURIComponent(raw);
    return value.length > 0 ? value : undefined;
  } catch (err: unknown) {
    if (err instanceof URIError) return undefined;
    throw err;
  }
}

/** Parse `key/<id>` pairs after the library sub-page. Unknown keys, empty ids and duplicates are dropped. */
export function parseLibraryDetail(segments: readonly string[]): LibraryDetail | undefined {
  const detail: LibraryDetail = {};
  for (let i = 0; i + 1 < segments.length; i += 2) {
    const id = decodeSegment(segments[i + 1]);
    if (id === undefined) continue;
    switch (segments[i]) {
      case 'artist':
        if (detail.artistId === undefined && detail.collectionId === undefined) detail.artistId = id;
        break;
      case 'collection':
        if (detail.artistId === undefined && detail.collectionId === undefined) detail.collectionId = id;
        break;
      case 'album':
        if (detail.albumId === undefined) detail.albumId = id;
        break;
      default:
        break;
    }
  }
  return detail.artistId !== undefined || detail.collectionId !== undefined || detail.albumId !== undefined ? detail : undefined;
}

/** Retired leaves that now live as sections of the Profiles page; old bookmarks are rewritten to it by `readRoute`. */
const LEGACY_PROFILE_LEAVES: readonly string[] = ['release-profiles', 'metadata-profiles'];

/** Parse and validate a hash; anything unknown falls back (per segment) to the nearest valid default. */
export function parseRouteHash(hash: string): AppRoute | null {
  const trimmed = hash.trim();
  if (trimmed.startsWith('#import') || trimmed.startsWith('#/import')) {
    const pending = parseImportHash(trimmed);
    if (pending) {
      storePendingImport(pending);
    }
    return { tab: 'playlists' };
  }

  const segments = hash.replace(/^#\/?/, '').split('/').filter((s) => s.length > 0);
  const tab = pick(MAIN_TABS, segments[0]);
  if (!tab) return null;
  const sub = segments[1];
  switch (tab) {
    case 'discover': {
      const artistId = sub === 'artist' ? decodeSegment(segments[2]) : undefined;
      return artistId === undefined ? { tab } : { tab, artistId };
    }
    case 'requests': {
      const requestsSub = pick(REQUESTS_SUBS, sub) ?? 'all';
      const issueId = requestsSub === 'issues' ? decodeSegment(segments[2]) : undefined;
      return issueId === undefined ? { tab, sub: requestsSub } : { tab, sub: requestsSub, issueId };
    }
    case 'library': {
      const librarySub = pick(LIBRARY_SUBS, sub) ?? 'artists';
      const detail = parseLibraryDetail(segments.slice(2));
      return detail ? { tab, sub: librarySub, detail } : { tab, sub: librarySub };
    }
    case 'activity':
      return { tab, sub: pick(ACTIVITY_SUBS, sub) ?? 'queue' };
    case 'wanted':
      return { tab, sub: pick(WANTED_SUBS, sub) ?? 'missing' };
    case 'settings':
      return settingsRouteFor(pick(SETTINGS_SECTIONS, sub) ?? 'general', LEGACY_PROFILE_LEAVES.includes(segments[2] ?? '') ? 'profiles' : segments[2]);
    default:
      return { tab };
  }
}

/** OAuth return trips (`?connected`, `?scrobble_error`) land on Settings > Requests > Scrobbling. */
function readRoute(initial: boolean): AppRoute {
  const fromHash = parseRouteHash(window.location.hash);
  if (fromHash) {
    // Rewrite a non-canonical hash (`#/library/foo`) in place, without adding a history entry.
    const canonical = routeToHash(fromHash);
    if (window.location.hash !== canonical) {
      window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}${canonical}`);
    }
    return fromHash;
  }
  // Only the first load can be an OAuth return trip; later popstate/hashchange must not re-route.
  // The params are left in place: useScrobbling reads and strips them when the scrobbling page mounts.
  if (initial) {
    if (peekPendingImport()) {
      const canonical = routeToHash({ tab: 'playlists' });
      if (window.location.hash !== canonical) {
        window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}${canonical}`);
      }
      return { tab: 'playlists' };
    }
    const q = new URLSearchParams(window.location.search);
    if (q.has('connected') || q.has('scrobble_error') || q.has('lastfm_state')) return settingsRouteFor('requests', 'scrobbling');
  }
  return defaultRoute('discover');
}

export interface UseAppRouteReturn {
  route: AppRoute;
  navigate: Navigate;
  /**
   * Go to `parent` as an "up" step: `history.back()` when the previous entry is that parent (so Back and the on-screen
   * button agree), otherwise replace the current entry with it (deep links never exit the app).
   */
  navigateUp: (parent: AppRoute) => void;
}

/** Single source of truth for the current page, mirrored to the URL hash so every page is deep-linkable. */
export function useAppRoute(): UseAppRouteReturn {
  const [route, setRoute] = useState<AppRoute>(() => readRoute(true));

  useEffect(() => {
    // Keep the existing route object when the hash did not change (modal sentinel pops fire popstate too).
    const sync = (): void =>
      setRoute((current) => {
        const next = readRoute(false);
        return routeToHash(next) === routeToHash(current) ? current : next;
      });
    window.addEventListener('popstate', sync);
    window.addEventListener('hashchange', sync);
    return () => {
      window.removeEventListener('popstate', sync);
      window.removeEventListener('hashchange', sync);
    };
  }, []);

  const navigate = useCallback<Navigate>((next, options) => {
    const hash = routeToHash(next);
    if (window.location.hash !== hash) {
      const url = `${window.location.pathname}${window.location.search}${hash}`;
      if (options?.replace) {
        // Replacing keeps the same previous entry.
        const prev = prevHashOf(window.history.state);
        window.history.replaceState(prev === undefined ? null : { [PREV_KEY]: prev }, '', url);
      } else {
        window.history.pushState({ [PREV_KEY]: window.location.hash }, '', url);
      }
    }
    setRoute(next);
  }, []);

  const navigateUp = useCallback(
    (parent: AppRoute): void => {
      if (prevHashOf(window.history.state) === routeToHash(parent)) {
        window.history.back();
        return;
      }
      navigate(parent, { replace: true });
    },
    [navigate]
  );

  return { route, navigate, navigateUp };
}
