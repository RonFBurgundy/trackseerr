import React, { useState } from 'react';
import { RotateCcw } from 'lucide-react';
import { ConfirmDialog, MachinedCard, ScrollFill, TapeDeckButton } from '@/components/ui';
import { useQualityDefinitions } from '@/hooks/useQualityDefinitions';
import { EmptyNote } from './ProfileSection';
import { QualityDefinitionRow } from './QualityDefinitionRow';
import { ImportBitrateCheckSelect } from './ImportBitrateCheckSelect';

export interface QualityDefinitionsPanelProps {
  enabled: boolean;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Settings > Media Management > Quality: size-per-length (kbps) bounds for every quality. */
export const QualityDefinitionsPanel: React.FC<QualityDefinitionsPanelProps> = ({ enabled, onToast }) => {
  const { definitions, loading, saveRow, resetRow, resetAll } = useQualityDefinitions(enabled, onToast);
  const [confirmAll, setConfirmAll] = useState<boolean>(false);
  const [busy, setBusy] = useState<boolean>(false);

  const doResetAll = async (): Promise<void> => {
    setBusy(true);
    await resetAll();
    setBusy(false);
    setConfirmAll(false);
  };

  return (
    <div className="flex min-h-0 flex-col gap-3">
      <MachinedCard className="p-3 sm:p-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="space-y-1.5 text-xs font-mono text-neutral-400">
            <p>
              Limits are in <span className="text-neutral-200">kbps</span>: release size &divide; album length. Leave a bound blank for no
              limit. Preferred only ranks releases of the same quality (closer wins); it never rejects.
            </p>
            <p>
              Measured as the average bitrate over the release&apos;s length. Applies to whole releases at grab time and to each track file on import.
            </p>
            <p>
              Drag the handles (min, preferred, max) or type exact kbps; MB/min = kbps &times; 0.0075. A max at the far right means unbounded; faint ticks
              mark the shipped defaults.
            </p>
            <p>
              Values marked derived in the research (<span className="text-neutral-200">docs/QUALITY_RESEARCH.md</span>) are suggestions, not
              community standards: lossy minimums in particular are our addition, so loosen them if good releases get rejected. When album length
              is unknown only the max is enforced.
            </p>
          </div>
          <TapeDeckButton size="sm" className="shrink-0" onClick={() => setConfirmAll(true)} icon={<RotateCcw className="h-3.5 w-3.5" />}>
            Reset all
          </TapeDeckButton>
        </div>
      </MachinedCard>

      {loading && definitions.length === 0 ? (
        <EmptyNote>Loading quality definitions...</EmptyNote>
      ) : definitions.length === 0 ? (
        <EmptyNote>No quality definitions found.</EmptyNote>
      ) : (
        <ScrollFill ariaLabel="Quality definitions" className="grid grid-cols-1 gap-2.5 content-start lg:grid-cols-2">
          {definitions.map((d) => (
            <QualityDefinitionRow
              key={`${d.quality}:${d.min_kbps}:${d.preferred_kbps}:${d.max_kbps}`}
              definition={d}
              onSave={saveRow}
              onReset={resetRow}
            />
          ))}
        </ScrollFill>
      )}

      <ImportBitrateCheckSelect enabled={enabled} onToast={onToast} />

      <ConfirmDialog
        isOpen={confirmAll}
        title="Reset all qualities"
        confirmLabel="Reset all"
        busy={busy}
        onConfirm={() => void doResetAll()}
        onCancel={() => setConfirmAll(false)}
      >
        Every quality returns to its shipped min / preferred / max. Your edits are lost.
      </ConfirmDialog>
    </div>
  );
};
