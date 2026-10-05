import { useCallback, useMemo, useState } from 'react';
import type { DelayProfile, DelayProfileInput } from '@/types/delayProfiles';
import type { ReleaseProtocol } from '@/types/qualityProfiles';

export interface DelayDraft {
  name: string;
  preferred: ReleaseProtocol;
  usenet: string;
  torrent: string;
  soulseek: string;
  bypassHighest: boolean;
  bypassScore: string;
  tags: string[];
}

const NON_NEGATIVE_INT = /^\d+$/;
const INT = /^-?\d+$/;

export interface UseDelayProfileDraftReturn {
  draft: DelayDraft;
  patch: (change: Partial<DelayDraft>) => void;
  problem: string | null;
  toInput: () => DelayProfileInput | null;
}

/** Editable copy of one delay profile (or a blank one). Delays are minutes; blank bypass score = never bypass on score. */
export function useDelayProfileDraft(profile: DelayProfile | null): UseDelayProfileDraftReturn {
  const [draft, setDraft] = useState<DelayDraft>(() => ({
    name: profile?.name ?? '',
    preferred: profile?.preferred_protocol ?? 'usenet',
    usenet: String(profile?.delays.usenet ?? 0),
    torrent: String(profile?.delays.torrent ?? 0),
    soulseek: String(profile?.delays.soulseek ?? 0),
    bypassHighest: profile?.bypass_if_highest_quality ?? true,
    bypassScore: profile?.bypass_if_above_score === null || profile?.bypass_if_above_score === undefined ? '' : String(profile.bypass_if_above_score),
    tags: profile?.tags ?? [],
  }));

  const patch = useCallback((change: Partial<DelayDraft>): void => setDraft((prev) => ({ ...prev, ...change })), []);

  const validated = useMemo((): { input: DelayProfileInput | null; problem: string | null } => {
    const isDefault = profile?.is_default ?? false;
    if (!draft.name.trim() && !isDefault) return { input: null, problem: 'Give the profile a name.' };
    const delays = [draft.usenet, draft.torrent, draft.soulseek].map((t) => t.trim());
    if (!delays.every((t) => NON_NEGATIVE_INT.test(t))) return { input: null, problem: 'Delays must be whole minutes (0 or more).' };
    const score = draft.bypassScore.trim();
    if (score !== '' && !INT.test(score)) return { input: null, problem: 'Bypass score must be a whole number or blank.' };
    return {
      problem: null,
      input: {
        name: draft.name.trim() || (profile?.name ?? ''),
        preferred_protocol: draft.preferred,
        delays: { usenet: Number(delays[0]), torrent: Number(delays[1]), soulseek: Number(delays[2]) },
        bypass_if_highest_quality: draft.bypassHighest,
        bypass_if_above_score: score === '' ? null : Number(score),
        tags: isDefault ? [] : draft.tags,
      },
    };
  }, [draft, profile]);

  const toInput = useCallback((): DelayProfileInput | null => validated.input, [validated]);

  return { draft, patch, problem: validated.problem, toInput };
}
