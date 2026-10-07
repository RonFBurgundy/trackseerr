import type { Narrow, Schema } from './apiSchema';
/**
 * Native-library metadata profiles (optional, off by default). A profile only shapes AUTOMATIC monitoring: it never
 * hides a release from the catalog and never blocks a manual monitor or request.
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

export type MetadataProfile = Narrow<Schema<'MetadataProfile'>, { primary_types: ReleasePrimaryType[]; secondary_types: ReleaseSecondaryType[] }>;

export type MetadataProfileList = Narrow<Schema<'MetadataProfilesResponse'>, { profiles: MetadataProfile[] }>;

export interface MetadataProfileInput {
  name: string;
  primary_types: ReleasePrimaryType[];
  secondary_types: ReleaseSecondaryType[];
}

export type MetadataProfileDeleteResult = Schema<'MetadataProfileDeleteResponse'>;

export type MetadataProfileWouldChange = Schema<'MetadataProfileChange'>;

export type MetadataProfilePreview = Schema<'MetadataProfilePreview'>;

/** Editor target: an existing profile, or `'new'` for a blank one. */
export type ReleaseOrNew = MetadataProfile | 'new';
