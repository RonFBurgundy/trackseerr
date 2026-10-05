import React, { useId, useState } from 'react';
import { FlaskConical, Loader2 } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';
import { RELEASE_PROTOCOLS, type ReleaseProtocol } from '@/types/qualityProfiles';
import { useReleaseEvaluation } from '@/hooks/useReleaseEvaluation';
import { compactInputClass, compactLabelClass } from '@/components/settings/formClasses';
import { BreakdownView } from './BreakdownView';

const ANY = '';

function isProtocol(value: string): value is ReleaseProtocol {
  return RELEASE_PROTOCOLS.some((p) => p === value);
}

/** "Test a release": title + size + protocol evaluated against the saved version of this profile. */
export const ReleaseTester: React.FC<{ profileId: string | null }> = ({ profileId }) => {
  const uid = useId();
  const { result, error, running, run } = useReleaseEvaluation(profileId);
  const [title, setTitle] = useState<string>('');
  const [sizeMb, setSizeMb] = useState<string>('');
  const [protocol, setProtocol] = useState<string>(ANY);

  if (profileId === null) {
    return <p className="text-xs font-mono text-neutral-500">Save the profile first, then reopen it to test releases against it.</p>;
  }

  return (
    <div className="space-y-2">
      <p className="text-[11px] font-mono text-[var(--text-muted)]">Runs against the last saved version of this profile.</p>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-6">
        <div className="col-span-2 sm:col-span-6">
          <label htmlFor={`${uid}-title`} className={compactLabelClass}>
            Release title
          </label>
          <input
            id={`${uid}-title`}
            name="test-release-title"
            type="text"
            autoComplete="off"
            placeholder="Artist - Album (2020) [FLAC 24bit Vinyl]"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className={compactInputClass}
          />
        </div>
        <div className="sm:col-span-3">
          <label htmlFor={`${uid}-size`} className={compactLabelClass}>
            Size (MB)
          </label>
          <input
            id={`${uid}-size`}
            name="test-release-size-mb"
            type="number"
            inputMode="decimal"
            min={0}
            placeholder="unknown"
            value={sizeMb}
            onChange={(e) => setSizeMb(e.target.value)}
            className={`${compactInputClass} font-mono`}
          />
        </div>
        <div className="sm:col-span-3">
          <label htmlFor={`${uid}-protocol`} className={compactLabelClass}>
            Protocol
          </label>
          <select
            id={`${uid}-protocol`}
            name="test-release-protocol"
            value={protocol}
            onChange={(e) => setProtocol(e.target.value)}
            className={compactInputClass}
          >
            <option value={ANY}>Any</option>
            {RELEASE_PROTOCOLS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>
      </div>
      <TapeDeckButton
        size="sm"
        variant="amber"
        disabled={running || title.trim() === ''}
        onClick={() => void run(title, sizeMb, isProtocol(protocol) ? protocol : null)}
        icon={running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FlaskConical className="h-3.5 w-3.5" />}
      >
        Evaluate
      </TapeDeckButton>
      {error && (
        <p role="alert" className="text-[11px] font-mono text-[var(--status-error)]">
          {error}
        </p>
      )}
      {result && <BreakdownView result={result} />}
    </div>
  );
};
