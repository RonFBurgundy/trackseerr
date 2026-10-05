import { useCallback, useState } from 'react';
import type { ReleaseEvaluation, ReleaseProtocol } from '@/types/qualityProfiles';
import { evaluateRelease } from '@/services/qualityProfileService';
import { errorMessage } from '@/services/apiClient';

export interface UseReleaseEvaluationReturn {
  result: ReleaseEvaluation | null;
  error: string | null;
  running: boolean;
  /** `sizeMb` blank = unknown size; `protocol` null = unspecified. */
  run: (title: string, sizeMb: string, protocol: ReleaseProtocol | null) => Promise<void>;
}

/** "Test a release": evaluates a title/size/protocol against one saved quality profile. */
export function useReleaseEvaluation(profileId: string | null): UseReleaseEvaluationReturn {
  const [result, setResult] = useState<ReleaseEvaluation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState<boolean>(false);

  const run = useCallback(
    async (title: string, sizeMb: string, protocol: ReleaseProtocol | null): Promise<void> => {
      if (profileId === null) return;
      const mb = sizeMb.trim() === '' ? null : Number(sizeMb);
      if (mb !== null && (!Number.isFinite(mb) || mb < 0)) {
        setError('Size must be a positive number of MB.');
        return;
      }
      setRunning(true);
      setError(null);
      try {
        setResult(
          await evaluateRelease({
            title: title.trim(),
            profile_id: profileId,
            ...(mb !== null ? { size_bytes: Math.round(mb * 1024 * 1024) } : {}),
            ...(protocol ? { protocol } : {}),
          })
        );
      } catch (err: unknown) {
        setResult(null);
        setError(errorMessage(err, 'Evaluation failed'));
      } finally {
        setRunning(false);
      }
    },
    [profileId]
  );

  return { result, error, running, run };
}
