import type { ReleaseProtocol } from './qualityProfiles';

export interface DelayMinutes {
  usenet: number;
  torrent: number;
  soulseek: number;
}

export interface DelayProfile {
  id: number;
  order: number;
  name: string;
  preferred_protocol: ReleaseProtocol;
  delays: DelayMinutes;
  bypass_if_highest_quality: boolean;
  bypass_if_above_score: number | null;
  tags: string[];
  is_default: boolean;
}

export interface DelayProfileInput {
  name: string;
  preferred_protocol: ReleaseProtocol;
  delays: DelayMinutes;
  bypass_if_highest_quality: boolean;
  bypass_if_above_score: number | null;
  tags: string[];
}

export interface PendingRelease {
  id: number;
  title: string;
  album_id: string | number | null;
  artist_name: string | null;
  protocol: string;
  quality: string | null;
  format_score: number | null;
  added_at: string;
  release_at: string;
  reason: string | null;
}
