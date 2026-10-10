import type { Schema } from './apiSchema';
import type { Playlist } from './models';

export type SmartSortOption = Schema<'SmartRulesBody'>['sort'];

export const SMART_SORT_LABELS: Record<SmartSortOption, string> = {
  random: 'Random',
  year_desc: 'Newest first',
  year_asc: 'Oldest first',
  popularity_desc: 'Most popular',
  added_desc: 'Recently added',
  artist: 'By artist',
};

export function isSmartCollection(pl: Playlist): boolean {
  return pl.service === 'trackseerr' && pl.source_kind === 'smart';
}
