import React from 'react';
import type { CustomFormat } from '@/types/customFormats';
import { compactInputClass } from '@/components/settings/formClasses';

export interface FormatScoreTableProps {
  formats: readonly CustomFormat[];
  scores: Readonly<Record<number, string>>;
  onScore: (formatId: number, value: string) => void;
}

/** One score input per custom format (blank = 0). Higher scores win ties within a quality; negative scores penalise. */
export const FormatScoreTable: React.FC<FormatScoreTableProps> = ({ formats, scores, onScore }) => {
  if (formats.length === 0) {
    return <p className="text-xs font-mono text-neutral-500">No custom formats yet. Create some on the Custom Formats page to score releases.</p>;
  }
  return (
    <ul className="max-h-64 divide-y divide-[var(--border-subtle)] overflow-y-auto rounded-[4px] border border-[var(--border-default)] bg-[var(--bg-card)]">
      {formats.map((f) => {
        const id = `format-score-${f.id}`;
        return (
          <li key={f.id} className="flex items-center justify-between gap-3 px-2.5 py-1">
            <label htmlFor={id} className="min-w-0 flex-1 truncate text-[13px] text-neutral-200">
              {f.name}
            </label>
            <input
              id={id}
              name={`format-score-${f.id}`}
              type="number"
              inputMode="numeric"
              step={1}
              placeholder="0"
              value={scores[f.id] ?? ''}
              onChange={(e) => onScore(f.id, e.target.value)}
              className={`${compactInputClass} w-24 shrink-0 text-right font-mono`}
            />
          </li>
        );
      })}
    </ul>
  );
};
