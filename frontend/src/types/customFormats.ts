/** Custom formats (Lidarr/Servarr JSON schema compatible). */
export type SpecImplementation =
  | 'ReleaseTitleSpecification'
  | 'ReleaseGroupSpecification'
  | 'SizeSpecification'
  | 'IndexerFlagSpecification'
  | 'QualitySpecification'
  | 'SourceSpecification'
  | 'PhraseSpecification'
  | 'ProtocolSpecification';

export interface SpecTypeInfo {
  implementation: SpecImplementation;
  label: string;
  hint: string;
}

/** Editable spec types in picker order. Anything else the backend stores is shown read-only. */
export const SPEC_TYPES: readonly SpecTypeInfo[] = [
  { implementation: 'ReleaseTitleSpecification', label: 'Release Title', hint: 'Case-insensitive regex tested against the release title.' },
  { implementation: 'ReleaseGroupSpecification', label: 'Release Group', hint: 'Case-insensitive regex tested against the release group.' },
  { implementation: 'SizeSpecification', label: 'Size', hint: 'Whole-release size in GB; matches when min < size <= max. Leave max blank for no upper bound.' },
  { implementation: 'IndexerFlagSpecification', label: 'Indexer Flag', hint: 'Integer indexer flag value (for example 1 = freeleech on many trackers).' },
  { implementation: 'QualitySpecification', label: 'Quality', hint: 'Matches when the parsed quality is exactly this one.' },
  { implementation: 'SourceSpecification', label: 'Source', hint: 'Media source parsed from the title.' },
  { implementation: 'PhraseSpecification', label: 'Phrase', hint: 'Fuzzy phrase match (token-set ratio at or above the threshold).' },
  { implementation: 'ProtocolSpecification', label: 'Protocol', hint: 'Matches releases from one download protocol.' },
];

export const SPEC_SOURCES: readonly string[] = ['CD', 'WEB', 'Vinyl', 'SACD', 'Cassette', 'Unknown'];

export type SpecFieldValue = string | number | boolean | null;

export interface CustomFormatSpec {
  name: string;
  implementation: string;
  negate: boolean;
  required: boolean;
  fields: Record<string, SpecFieldValue>;
  unsupported?: boolean;
}

export interface CustomFormat {
  id: number;
  name: string;
  include_in_rename: boolean;
  specifications: CustomFormatSpec[];
  unsupported: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface CustomFormatInput {
  name: string;
  include_in_rename: boolean;
  specifications: Array<{
    name: string;
    implementation: string;
    negate: boolean;
    required: boolean;
    fields: Record<string, SpecFieldValue>;
  }>;
}

export interface CustomFormatImportEntry extends CustomFormat {
  action: 'created' | 'updated';
}

export interface CustomFormatImportError {
  index: number;
  name: string | null;
  errors: string[];
}

export interface CustomFormatImportResult {
  imported: CustomFormatImportEntry[];
  errors: CustomFormatImportError[];
}
