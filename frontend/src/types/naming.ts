import type { Narrow, Schema } from './apiSchema';
/** Lidarr-style naming formats: shared types for the naming editor and preview API. */

export interface NamingFormats {
  /** Single folder below the library root, e.g. `{Artist CleanName}`. */
  artist_folder_format: string;
  /** '/'-separated path below the artist folder: album folder(s) then file name. */
  standard_track_format: string;
  /** Same as standard, used for releases with more than one disc. */
  multi_disc_track_format: string;
  /** Optional file-name override for compilation / Various Artists tracks ('' = use the track format). */
  compilation_track_format?: string;
}

/** Fields applied together when a preset is chosen (never the library root path). */
export interface NamingPreset extends NamingFormats {
  colon_replacement_format?: string;
  clean_artist_names?: boolean;
}

export type NamingFormatKey = 'artist_folder_format' | 'standard_track_format' | 'multi_disc_track_format';

export type NamingFormatSample = Schema<'FormatSamplePreviewModel'>;

export type NamingFormatPreview = Narrow<Schema<'FormatPreviewModel'>, { samples: NamingFormatSample[]; warnings: string[] }>;

export type NamingPreviewResponse = Narrow<Schema<'PreviewResponseModel'>, { format_previews: Record<NamingFormatKey, NamingFormatPreview> }>;

export interface NamingTokenHelp {
  token: string;
  description: string;
  example: string;
}

export interface NamingTokenGroup {
  group: string;
  tokens: NamingTokenHelp[];
}

export interface NamingSyntaxHelp {
  syntax: string;
  description: string;
  example: string;
}

export interface NamingPresetCatalog {
  presets: Record<string, NamingPreset>;
  descriptions: Record<string, string>;
  tokenHelp: NamingTokenGroup[];
  syntaxHelp: NamingSyntaxHelp[];
}

/** Settings that influence how a format renders and must be sent along with a preview request. */
export interface NamingPreviewContext {
  root_folder_path?: string;
  colon_replacement_format?: string;
  clean_artist_names?: boolean;
}
