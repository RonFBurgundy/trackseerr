import React from 'react';
import type { ReleaseEvaluation } from '@/types/qualityProfiles';
import { Badge } from './ProfileSection';

const signed = (n: number): string => (n > 0 ? `+${n}` : String(n));

function kbpsLine(b: NonNullable<ReleaseEvaluation['breakdown']>['kbps']): string {
  if (!b.checked || b.measured === null) return b.skipped_reason ? `not checked (${b.skipped_reason})` : 'not checked';
  const bound = (v: number | null): string => (v === null ? 'any' : String(Math.round(v)));
  return `${Math.round(b.measured)} kbps${b.estimated ? ' (estimated length)' : ''} | min ${bound(b.min)} / pref ${bound(b.preferred)} / max ${bound(b.max)}`;
}

/** Renders the decision breakdown of /evaluate: verdict, quality tier, kbps, formats, release profiles, rejections, notes. */
export const BreakdownView: React.FC<{ result: ReleaseEvaluation }> = ({ result }) => {
  const b = result.breakdown;
  return (
    <div className="space-y-2 rounded-[4px] border border-[var(--border-default)] bg-[var(--bg-card)] p-2.5 text-xs font-mono" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={result.is_acceptable ? 'amber' : 'error'}>{result.is_acceptable ? 'Acceptable' : 'Rejected'}</Badge>
        <span className="text-neutral-300">Quality: {result.parsed_quality}</span>
        {b && (
          <span className="text-neutral-400">
            {b.quality_allowed ? `tier ${b.tier_name ?? (b.tier !== null ? b.tier + 1 : '?')}` : 'not allowed in profile'}
            {b.quality_cutoff_met ? ', cutoff met' : ''}
          </span>
        )}
      </div>
      {b && (
        <>
          <p className="text-neutral-400">Size: {kbpsLine(b.kbps)}</p>
          <div>
            <p className="text-neutral-300">
              Custom formats ({signed(b.format_score)}; min {b.min_format_score}, upgrade-until {b.cutoff_format_score})
            </p>
            {b.matched_formats.length === 0 ? (
              <p className="text-neutral-500">No format matched.</p>
            ) : (
              <ul className="mt-0.5 space-y-0.5">
                {b.matched_formats.map((f) => (
                  <li key={f.id} className="flex justify-between gap-3 text-neutral-200">
                    <span className="truncate">{f.name}</span>
                    <span className={f.score < 0 ? 'text-[var(--status-error)]' : 'text-[var(--status-success)]'}>{signed(f.score)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          {b.release_profiles.length > 0 && (
            <ul className="space-y-0.5 text-neutral-400">
              {b.release_profiles.map((r) => (
                <li key={r.id}>
                  Release profile {r.name}: {r.result}
                  {r.detail ? ` (${r.detail})` : ''}
                </li>
              ))}
            </ul>
          )}
          {b.rejections.length > 0 && (
            <ul className="space-y-0.5 text-[var(--status-error)]" aria-label="Rejections">
              {b.rejections.map((r) => (
                <li key={`${r.code}:${r.message}`}>{r.message}</li>
              ))}
            </ul>
          )}
          {b.notes.length > 0 && (
            <ul className="space-y-0.5 text-neutral-500" aria-label="Notes">
              {b.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          )}
        </>
      )}
      {!b && result.rejection_reasons.length > 0 && (
        <ul className="space-y-0.5 text-[var(--status-error)]">
          {result.rejection_reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
    </div>
  );
};
