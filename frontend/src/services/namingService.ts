import { apiRequest } from './apiClient';
import type { Narrow, Schema } from '@/types/apiSchema';
import type {
  NamingFormats,
  NamingPreset,
  NamingPresetCatalog,
  NamingSyntaxHelp,
  NamingTokenGroup,
  NamingPreviewContext,
  NamingPreviewResponse,
} from '@/types/naming';

/** The GET envelope with the loosely-typed help catalogues narrowed to what the UI renders. */
type MediaManagementEnvelope = Narrow<
  Schema<'MediaManagementGetResponse'>,
  { presets: Record<string, NamingPreset>; token_help?: NamingTokenGroup[]; syntax_help?: NamingSyntaxHelp[] }
>;

/** Loads the built-in naming presets (Trackseerr, TRaSH Guides, Plex, ...) and token help from the server. */
export async function getNamingPresets(): Promise<NamingPresetCatalog> {
  const res = await apiRequest<MediaManagementEnvelope>('/api/settings/media-management');
  return {
    presets: res?.presets ?? {},
    descriptions: res?.preset_descriptions ?? {},
    tokenHelp: res?.token_help ?? [],
    syntaxHelp: res?.syntax_help ?? [],
  };
}

/** Renders the given (unsaved) formats against every sample input; nothing is persisted. */
export async function previewNamingFormats(
  formats: NamingFormats,
  context: NamingPreviewContext = {}
): Promise<NamingPreviewResponse> {
  return apiRequest<NamingPreviewResponse>('/api/settings/media-management/preview', {
    method: 'POST',
    body: { ...context, ...formats },
  });
}
