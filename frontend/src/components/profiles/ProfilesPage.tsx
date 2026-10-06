import React from 'react';
import type { IndexerItem, LibraryManagerMode, MediaManagementSettings } from '@/types/models';
import { useCustomFormats } from '@/hooks/useCustomFormats';
import { useQualityDefinitions } from '@/hooks/useQualityDefinitions';
import { useQualityProfiles } from '@/hooks/useQualityProfiles';
import { DelayProfilesSection } from './DelayProfilesSection';
import { MetadataProfilesSection } from './MetadataProfilesSection';
import { QualityProfilesSection } from './QualityProfilesSection';
import { ReleaseProfilesSection } from './ReleaseProfilesSection';
import { TagsSection } from './TagsSection';

export interface ProfilesPageProps {
  /** False while Lidarr manages the library: native endpoints are not queried. */
  enabled: boolean;
  libraryMode: LibraryManagerMode;
  media: MediaManagementSettings | null;
  onMediaChange: React.Dispatch<React.SetStateAction<MediaManagementSettings | null>>;
  indexers: readonly IndexerItem[];
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Settings > Media Management > Profiles: five stacked sections inside one scrolling region (no extra tab row). */
export const ProfilesPage: React.FC<ProfilesPageProps> = ({ enabled, libraryMode, media, onMediaChange, indexers, onToast }) => {
  const qualityProfiles = useQualityProfiles(enabled, onToast);
  const definitions = useQualityDefinitions(enabled, onToast);
  const formats = useCustomFormats(enabled, onToast);

  return (
    <div className="space-y-8">
      <QualityProfilesSection manager={qualityProfiles} definitions={definitions.definitions} formats={formats.formats} />
      <MetadataProfilesSection enabled={enabled} settings={media} onChange={onMediaChange} onToast={onToast} />
      <DelayProfilesSection enabled={enabled} libraryMode={libraryMode} onToast={onToast} />
      <ReleaseProfilesSection enabled={enabled} libraryMode={libraryMode} indexers={indexers} qualityProfiles={qualityProfiles.profiles} onToast={onToast} />
      <TagsSection enabled={enabled} onToast={onToast} />
    </div>
  );
};
