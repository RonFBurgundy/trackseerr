/**
 * Native-library release profiles (optional, off by default). A profile only shapes AUTOMATIC monitoring: it never
 * hides a release from the catalog and never blocks a manual monitor or request. Not Lidarr's metadata profile.
 */
export type ReleasePrimaryType = 'album' | 'ep' | 'single' | 'broadcast' | 'other';

export type ReleaseSecondaryType =
  | 'studio'
  | 'compilation'
  | 'soundtrack'
  | 'spokenword'
  | 'interview'
  | 'audiobook'
  | 'audio drama'
  | 'live'
  | 'remix'
  | 'dj-mix'
  | 'mixtape/street'
  | 'demo'
  | 'field recording';

export const RELEASE_PRIMARY_TYPES: readonly ReleasePrimaryType[] = ['album', 'ep', 'single', 'broadcast', 'other'];

export const RELEASE_SECONDARY_TYPES: readonly ReleaseSecondaryType[] = [
  'studio',
  'compilation',
  'soundtrack',
  'spokenword',
  'interview',
  'audiobook',
  'audio drama',
  'live',
  'remix',
  'dj-mix',
  'mixtape/street',
  'demo',
  'field recording',
];

export const RELEASE_PRIMARY_LABELS: Readonly<Record<ReleasePrimaryType, string>> = {
  album: 'Album',
  ep: 'EP',
  single: 'Single',
  broadcast: 'Broadcast',
  other: 'Other',
};

export const RELEASE_SECONDARY_LABELS: Readonly<Record<ReleaseSecondaryType, string>> = {
  studio: 'Studio (no secondary type)',
  compilation: 'Compilation',
  soundtrack: 'Soundtrack',
  spokenword: 'Spokenword',
  interview: 'Interview',
  audiobook: 'Audiobook',
  'audio drama': 'Audio drama',
  live: 'Live',
  remix: 'Remix',
  'dj-mix': 'DJ-mix',
  'mixtape/street': 'Mixtape/Street',
  demo: 'Demo',
  'field recording': 'Field recording',
};

export interface ReleaseProfile {
  id: number;
  name: string;
  primary_types: ReleasePrimaryType[];
  secondary_types: ReleaseSecondaryType[];
  /** How many artists use this profile. */
  artist_count: number;
  created_at?: string;
  updated_at?: string;
}

export interface ReleaseProfileList {
  profiles: ReleaseProfile[];
  default_profile_id: number | null;
}

export interface ReleaseProfileInput {
  name: string;
  primary_types: ReleasePrimaryType[];
  secondary_types: ReleaseSecondaryType[];
}

export interface ReleaseProfileDeleteResult {
  deleted: number;
  artists_cleared: number;
}

/** Dry run of applying a profile to an artist's existing albums and tracks. */
export interface ReleaseProfileWouldChange {
  albums_to_monitor: number;
  albums_to_unmonitor: number;
  tracks_to_monitor: number;
  tracks_to_unmonitor: number;
}

/** `{matching, total}` release-group counts of an artist's catalog under a profile, plus the apply dry run. */
export interface ReleaseProfilePreview {
  matching: number;
  total: number;
  would_change: ReleaseProfileWouldChange;
}

/** Editor target: an existing profile, or `'new'` for a blank one. */
export type ReleaseOrNew = ReleaseProfile | 'new';
