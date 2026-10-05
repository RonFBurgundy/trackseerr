import { useCallback, useEffect, useState } from 'react';
import type { WantedListName } from '@/types/activity';
import type { LibraryTab } from './useLibrary';
import type { RequestFilter } from './useRequests';

export type MainTab = 'discover' | 'requests' | 'library' | 'playlists' | 'activity' | 'wanted' | 'settings';

export type RequestsSub = RequestFilter | 'issues';
export type ActivitySub = 'queue' | 'history' | 'blocklist';
export type WantedSub = WantedListName;

export type SettingsSection = 'general' | 'media-management' | 'lidarr' | 'requests' | 'system' | 'account';
export type MediaManagementLeaf = 'media' | 'profiles' | 'metadata-profiles' | 'clients' | 'indexers' | 'import-lists' | 'media-server';
export type RequestsLeaf = 'users' | 'scrobbling';
export type SystemLeaf = 'status' | 'queue' | 'tasks' | 'events' | 'logs';
export type SettingsLeafId = MediaManagementLeaf | RequestsLeaf | SystemLeaf;

export type SettingsRoute =
  | { tab: 'settings'; sub: 'general' | 'lidarr' | 'account' }
  | { tab: 'settings'; sub: 'media-management'; leaf: MediaManagementLeaf }
  | { tab: 'settings'; sub: 'requests'; leaf: RequestsLeaf }
  | { tab: 'settings'; sub: 'system'; leaf: SystemLeaf };

/** Current location. `sub` is always present for tabs that have sub-pages; `leaf` only under settings sections with children. */
export type AppRoute =
  | { tab: 'discover' }
  | { tab: 'playlists' }
  | { tab: 'requests'; sub: RequestsSub }
  | { tab: 'library'; sub: LibraryTab }
  | { tab: 'activity'; sub: ActivitySub }
  | { tab: 'wanted'; sub: WantedSub }
  | SettingsRoute;

export interface NavigateOptions {
  /** Replace the current history entry instead of pushing a new one. */
  replace?: boolean;
}

export type Navigate = (route: AppRoute, options?: NavigateOptions) => void;

export const MAIN_TABS: readonly MainTab[] = ['discover', 'requests', 'library', 'playlists', 'activity', 'wanted', 'settings'];
export const REQUESTS_SUBS: readonly RequestsSub[] = ['all', 'pending', 'approved', 'fulfilled', 'rejected', 'issues'];
export const LIBRARY_SUBS: readonly LibraryTab[] = ['artists', 'albums', 'tracks', 'collections'];
export const ACTIVITY_SUBS: readonly ActivitySub[] = ['queue', 'history', 'blocklist'];
export const WANTED_SUBS: readonly WantedSub[] = ['missing', 'cutoff'];
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
  'profiles',
  'metadata-profiles',
  'clients',
  'indexers',
  'import-lists',
  'media-server',
];
export const REQUESTS_LEAVES: readonly RequestsLeaf[] = ['users', 'scrobbling'];
export const SYSTEM_LEAVES: readonly SystemLeaf[] = ['status', 'queue', 'tasks', 'events', 'logs'];

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
      return { tab: 'settings', sub: section, leaf: pick(SYSTEM_LEAVES, leaf) ?? 'status' };
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

/** `#/<tab>[/<sub>[/<leaf>]]` */
export function routeToHash(route: AppRoute): string {
  const parts: string[] = [route.tab];
  if ('sub' in route) parts.push(route.sub);
  if ('leaf' in route) parts.push(route.leaf);
  return `#/${parts.join('/')}`;
}

/** Pre-rename leaf id of Metadata Profiles; old bookmarks are rewritten to the new leaf by `readRoute`. */
const LEGACY_RELEASE_PROFILES_LEAF = 'release-profiles';

/** Parse and validate a hash; anything unknown falls back (per segment) to the nearest valid default. */
export function parseRouteHash(hash: string): AppRoute | null {
  const segments = hash.replace(/^#\/?/, '').split('/').filter((s) => s.length > 0);
  const tab = pick(MAIN_TABS, segments[0]);
  if (!tab) return null;
  const sub = segments[1];
  switch (tab) {
    case 'requests':
      return { tab, sub: pick(REQUESTS_SUBS, sub) ?? 'all' };
    case 'library':
      return { tab, sub: pick(LIBRARY_SUBS, sub) ?? 'artists' };
    case 'activity':
      return { tab, sub: pick(ACTIVITY_SUBS, sub) ?? 'queue' };
    case 'wanted':
      return { tab, sub: pick(WANTED_SUBS, sub) ?? 'missing' };
    case 'settings':
      return settingsRouteFor(pick(SETTINGS_SECTIONS, sub) ?? 'general', segments[2] === LEGACY_RELEASE_PROFILES_LEAF ? 'metadata-profiles' : segments[2]);
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
    const q = new URLSearchParams(window.location.search);
    if (q.has('connected') || q.has('scrobble_error')) return settingsRouteFor('requests', 'scrobbling');
  }
  return defaultRoute('discover');
}

export interface UseAppRouteReturn {
  route: AppRoute;
  navigate: Navigate;
}

/** Single source of truth for the current page, mirrored to the URL hash so every page is deep-linkable. */
export function useAppRoute(): UseAppRouteReturn {
  const [route, setRoute] = useState<AppRoute>(() => readRoute(true));

  useEffect(() => {
    const sync = (): void => setRoute(readRoute(false));
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
      if (options?.replace) window.history.replaceState(null, '', url);
      else window.history.pushState(null, '', url);
    }
    setRoute(next);
  }, []);

  return { route, navigate };
}
