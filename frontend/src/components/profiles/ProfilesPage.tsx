import React from 'react';
import { ScrollFill } from '@/components/ui';
import type { IndexerItem, MediaManagementSettings } from '@/types/models';
import { useCustomFormats } from '@/hooks/useCustomFormats';
import { useQualityDefinitions } from '@/hooks/useQualityDefinitions';
import { useQualityProfiles } from '@/hooks/useQualityProfiles';
import { DelayProfilesSection } from './DelayProfilesSection';
import { MetadataProfilesSection } from './MetadataProfilesSection';
import { QualityProfilesSection } from './QualityProfilesSection';
import { ReleaseProfilesSection } from './ReleaseProfilesSection';

export interface ProfilesPageProps {
  /** False while Lidarr manages the library: native endpoints are not queried. */
  enabled: boolean;
  media: MediaManagementSettings | null;
  onMediaChange: React.Dispatch<React.SetStateAction<MediaManagementSettings | null>>;
  indexers: readonly IndexerItem[];
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Settings > Media Management > Profiles: four stacked sections inside one scrolling region (no extra tab row). */
export const ProfilesPage: React.FC<ProfilesPageProps> = ({ enabled, media, onMediaChange, indexers, onToast }) => {
  const qualityProfiles = useQualityProfiles(enabled, onToast);
  const definitions = useQualityDefinitions(enabled, onToast);
  const formats = useCustomFormats(enabled, onToast);

  return (
    <ScrollFill ariaLabel="Profiles" className="space-y-8 pr-1">
      <QualityProfilesSection manager={qualityProfiles} definitions={definitions.definitions} formats={formats.formats} />
      <MetadataProfilesSection enabled={enabled} settings={media} onChange={onMediaChange} onToast={onToast} />
      <DelayProfilesSection enabled={enabled} onToast={onToast} />
      <ReleaseProfilesSection enabled={enabled} indexers={indexers} qualityProfiles={qualityProfiles.profiles} onToast={onToast} />
    </ScrollFill>
  );
};
