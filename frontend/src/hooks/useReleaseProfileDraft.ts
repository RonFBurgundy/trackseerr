import { useCallback, useState } from 'react';
import type { ReleaseProfile, ReleaseProfileInput } from '@/types/releaseProfiles';

export interface UseReleaseProfileDraftReturn {
  name: string;
  setName: (v: string) => void;
  enabled: boolean;
  setEnabled: (v: boolean) => void;
  required: string[];
  setRequired: (v: string[]) => void;
  ignored: string[];
  setIgnored: (v: string[]) => void;
  indexerIds: string[];
  toggleIndexer: (id: string) => void;
  qualityProfileIds: string[];
  toggleQualityProfile: (id: string) => void;
  problem: string | null;
  /** Pass the flushed token lists to include text typed but not yet committed. */
  toInput: (terms?: { required: string[]; ignored: string[] }) => ReleaseProfileInput | null;
}

const toggled = (list: string[], id: string): string[] => (list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);

/** Editable copy of one release profile (or a blank one). Empty indexer / quality-profile lists mean "all". */
export function useReleaseProfileDraft(profile: ReleaseProfile | null): UseReleaseProfileDraftReturn {
  const [name, setName] = useState<string>(profile?.name ?? '');
  const [enabled, setEnabled] = useState<boolean>(profile?.enabled ?? true);
  const [required, setRequired] = useState<string[]>(profile?.required ?? []);
  const [ignored, setIgnored] = useState<string[]>(profile?.ignored ?? []);
  const [indexerIds, setIndexerIds] = useState<string[]>(profile?.indexer_ids.map(String) ?? []);
  const [qualityProfileIds, setQualityProfileIds] = useState<string[]>(profile?.quality_profile_ids ?? []);

  const toggleIndexer = useCallback((id: string): void => setIndexerIds((prev) => toggled(prev, id)), []);
  const toggleQualityProfile = useCallback((id: string): void => setQualityProfileIds((prev) => toggled(prev, id)), []);

  const problem = name.trim() ? null : 'Give the profile a name.';

  const toInput = useCallback((terms?: { required: string[]; ignored: string[] }): ReleaseProfileInput | null => {
    if (problem) return null;
    return {
      name: name.trim(),
      enabled,
      required: terms?.required ?? required,
      ignored: terms?.ignored ?? ignored,
      indexer_ids: indexerIds,
      tags: profile?.tags.map(String) ?? [],
      quality_profile_ids: qualityProfileIds,
    };
  }, [problem, name, enabled, required, ignored, indexerIds, qualityProfileIds, profile]);

  return {
    name,
    setName,
    enabled,
    setEnabled,
    required,
    setRequired,
    ignored,
    setIgnored,
    indexerIds,
    toggleIndexer,
    qualityProfileIds,
    toggleQualityProfile,
    problem,
    toInput,
  };
}
