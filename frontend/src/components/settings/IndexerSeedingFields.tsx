import React, { useId } from 'react';
import type { SeedingDraft, SeedingDraftField } from '@/hooks/useIndexerDraft';
import { formatMinutes } from '@/components/lists';
import { compactInputClass, compactLabelClass } from './formClasses';

export interface IndexerSeedingFieldsProps {
  value: SeedingDraft;
  onChange: (field: SeedingDraftField, value: string) => void;
  /** Global limits shown in the "inherit" placeholders. */
  globalSeedRatio: number | null | undefined;
  globalSeedTimeMinutes: number | null | undefined;
}

const inheritText = (value: number | null | undefined, format: (n: number) => string): string =>
  value === null || value === undefined || value <= 0 ? 'Inherit global (none)' : `Inherit global (${format(value)})`;

const helper = (text: string): string => {
  const n = Number(text);
  return text.trim() !== '' && Number.isFinite(n) && n >= 60 ? `= ${formatMinutes(n)}` : '';
};

/** "Seeding" group of the indexer form; an empty input means inherit the global limit. */
export const IndexerSeedingFields: React.FC<IndexerSeedingFieldsProps> = ({
  value,
  onChange,
  globalSeedRatio,
  globalSeedTimeMinutes,
}) => {
  const ratioId = useId();
  const timeId = useId();
  const discoId = useId();
  const seedersId = useId();
  const timePlaceholder = inheritText(globalSeedTimeMinutes, (n) => `${n} min`);

  return (
    <fieldset className="pt-3 border-t border-[#1f1f1f] space-y-3 min-w-0">
      <legend className="text-xs font-bold uppercase font-mono text-white pr-2">Seeding</legend>
      <p className="text-[11px] text-neutral-400 font-mono">
        Private trackers often require a ratio or seed time. TrackSeerr pushes these limits to your torrent client and
        never removes a torrent before they&apos;re met.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label htmlFor={ratioId} className={compactLabelClass}>Seed ratio</label>
          <input
            id={ratioId}
            name="seed_ratio"
            type="number"
            inputMode="decimal"
            min={0}
            step={0.1}
            value={value.seed_ratio}
            onChange={(e) => onChange('seed_ratio', e.target.value)}
            placeholder={inheritText(globalSeedRatio, (n) => String(n))}
            className={compactInputClass}
          />
        </div>
        <div>
          <label htmlFor={seedersId} className={compactLabelClass}>Minimum seeders</label>
          <input
            id={seedersId}
            name="minimum_seeders"
            type="number"
            inputMode="numeric"
            min={0}
            step={1}
            value={value.minimum_seeders}
            onChange={(e) => onChange('minimum_seeders', e.target.value)}
            placeholder="Inherit global (none)"
            className={compactInputClass}
          />
        </div>
        <div>
          <label htmlFor={timeId} className={compactLabelClass}>Seed time (minutes)</label>
          <input
            id={timeId}
            name="seed_time_minutes"
            type="number"
            inputMode="numeric"
            min={0}
            step={1}
            value={value.seed_time_minutes}
            onChange={(e) => onChange('seed_time_minutes', e.target.value)}
            placeholder={timePlaceholder}
            className={compactInputClass}
          />
          <p className="text-[10px] font-mono text-neutral-500 mt-1 min-h-[14px]">{helper(value.seed_time_minutes)}</p>
        </div>
        <div>
          <label htmlFor={discoId} className={compactLabelClass}>Discography seed time (minutes)</label>
          <input
            id={discoId}
            name="discography_seed_time_minutes"
            type="number"
            inputMode="numeric"
            min={0}
            step={1}
            value={value.discography_seed_time_minutes}
            onChange={(e) => onChange('discography_seed_time_minutes', e.target.value)}
            placeholder={timePlaceholder}
            className={compactInputClass}
          />
          <p className="text-[10px] font-mono text-neutral-500 mt-1 min-h-[14px]">
            {helper(value.discography_seed_time_minutes)}
          </p>
        </div>
      </div>
    </fieldset>
  );
};
