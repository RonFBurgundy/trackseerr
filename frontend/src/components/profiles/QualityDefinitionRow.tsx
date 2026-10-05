import React, { useId, useState } from 'react';
import { Loader2, RotateCcw, Save } from 'lucide-react';
import { MachinedCard, TapeDeckButton } from '@/components/ui';
import type { QualityDefinition, QualityDefinitionInput } from '@/types/qualityDefinitions';
import { compactInputClass, compactLabelClass } from '@/components/settings/formClasses';
import { KbpsSlider } from './KbpsSlider';
import { formatMbPerMin, scaleFor } from './qualityScales';

const MAX_KBPS = 100000;

type Bounds = QualityDefinitionInput;

/** Blank = unbounded (null); anything else must be a number in range. Returns undefined for an invalid entry. */
function parseBound(raw: string): number | null | undefined {
  const text = raw.trim();
  if (text === '') return null;
  const n = Number(text);
  if (!Number.isFinite(n) || n < 0 || n > MAX_KBPS) return undefined;
  return n;
}

/** Mirrors the backend's rules: min <= preferred, and (when max is a positive number) min, preferred <= max. */
export function validateBounds(min: string, preferred: string, max: string): { bounds: Bounds | null; error: string | null } {
  const lo = parseBound(min);
  const pref = parseBound(preferred);
  const hi = parseBound(max);
  if (lo === undefined || pref === undefined || hi === undefined) {
    return { bounds: null, error: `Enter a number between 0 and ${MAX_KBPS}, or leave blank for unbounded.` };
  }
  if (lo !== null && pref !== null && lo > pref) return { bounds: null, error: 'Min must not exceed preferred.' };
  if (hi !== null && hi > 0) {
    if (lo !== null && lo > hi) return { bounds: null, error: 'Min must not exceed max.' };
    if (pref !== null && pref > hi) return { bounds: null, error: 'Preferred must not exceed max.' };
  }
  return { bounds: { min_kbps: lo, preferred_kbps: pref, max_kbps: hi }, error: null };
}

/** Valid non-blank number, else null (blank and invalid both render as unbounded on the slider). */
const numeric = (raw: string): number | null => {
  const v = parseBound(raw);
  return v === undefined ? null : v;
};

const text = (v: number | null): string => (v === null ? '' : String(v));

export interface QualityDefinitionRowProps {
  definition: QualityDefinition;
  onSave: (quality: string, input: QualityDefinitionInput) => Promise<string | null>;
  onReset: (quality: string) => Promise<string | null>;
}

/** One quality: three kbps inputs, a default-vs-current range bar, per-row save and reset. Remount to re-sync. */
export const QualityDefinitionRow: React.FC<QualityDefinitionRowProps> = ({ definition: d, onSave, onReset }) => {
  const uid = useId();
  const [min, setMin] = useState<string>(text(d.min_kbps));
  const [preferred, setPreferred] = useState<string>(text(d.preferred_kbps));
  const [max, setMax] = useState<string>(text(d.max_kbps));
  const [busy, setBusy] = useState<boolean>(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const { bounds, error } = validateBounds(min, preferred, max);
  const dirty = min !== text(d.min_kbps) || preferred !== text(d.preferred_kbps) || max !== text(d.max_kbps);

  const run = async (action: () => Promise<string | null>): Promise<boolean> => {
    setBusy(true);
    setServerError(null);
    const err = await action();
    setBusy(false);
    if (err) setServerError(err);
    return err === null;
  };

  const resetRow = async (): Promise<void> => {
    const ok = await run(() => onReset(d.quality));
    if (!ok) return;
    setMin(text(d.default_min_kbps));
    setPreferred(text(d.default_preferred_kbps));
    setMax(text(d.default_max_kbps));
  };

  const fields: Array<{ key: string; label: string; value: string; set: (v: string) => void }> = [
    { key: 'min', label: 'Min kbps', value: min, set: setMin },
    { key: 'preferred', label: 'Preferred', value: preferred, set: setPreferred },
    { key: 'max', label: 'Max kbps', value: max, set: setMax },
  ];

  return (
    <MachinedCard className="p-2.5 sm:p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-bold text-white">{d.title}</span>
        <div className="flex items-center gap-1.5">
          <TapeDeckButton
            size="sm"
            aria-label={`Reset ${d.title} to default`}
            disabled={busy || (d.is_default && !dirty)}
            onClick={() => void resetRow()}
            icon={<RotateCcw className="h-3.5 w-3.5" />}
          >
            Reset
          </TapeDeckButton>
          <TapeDeckButton
            size="sm"
            variant="amber"
            aria-label={`Save ${d.title}`}
            disabled={busy || !dirty || bounds === null}
            onClick={() => bounds && void run(() => onSave(d.quality, bounds))}
            icon={busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
          >
            Save
          </TapeDeckButton>
        </div>
      </div>
      <div className="grid grid-cols-3 gap-2">
        {fields.map((f) => (
          <div key={f.key}>
            <label htmlFor={`${uid}-${f.key}`} className={compactLabelClass}>
              {f.label}
              <span className="ml-1 text-neutral-500">{numeric(f.value) === null ? 'unbounded' : formatMbPerMin(numeric(f.value) ?? 0)}</span>
            </label>
            <input
              id={`${uid}-${f.key}`}
              name={`${d.quality}-${f.key}-kbps`}
              type="number"
              inputMode="decimal"
              min={0}
              max={MAX_KBPS}
              step="any"
              placeholder="none"
              value={f.value}
              onChange={(e) => f.set(e.target.value)}
              className={`${compactInputClass} font-mono`}
            />
          </div>
        ))}
      </div>
      <KbpsSlider
        label={d.title}
        name={d.quality}
        scale={scaleFor(d.quality, d.default_max_kbps)}
        values={{ min: numeric(min), preferred: numeric(preferred), max: numeric(max) }}
        defaults={{ min: d.default_min_kbps, preferred: d.default_preferred_kbps, max: d.default_max_kbps }}
        onChange={(field, v) => (field === 'min' ? setMin(v) : field === 'preferred' ? setPreferred(v) : setMax(v))}
      />
      {(error || serverError) && (
        <p role="alert" className="text-[11px] font-mono text-[var(--status-error)]">
          {serverError ?? error}
        </p>
      )}
    </MachinedCard>
  );
};
