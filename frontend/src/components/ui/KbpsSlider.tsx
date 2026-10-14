import React, { useId, useState } from 'react';

export interface KbpsValues {
  min: number | null;
  preferred: number | null;
  max: number | null;
}

export interface KbpsSliderProps {
  /** Label prefix for the three handles, e.g. the quality title. */
  label: string;
  /** Name prefix for the inputs. */
  name: string;
  /** Upper end of the track in kbps (grows if a stored value exceeds it). */
  scale: number;
  values: KbpsValues;
  defaults: KbpsValues;
  /** Receives the new bound as text ('' = unbounded) so the row's number inputs stay in step. */
  onChange: (field: 'min' | 'preferred' | 'max', text: string) => void;
}

type Handle = 'min' | 'preferred' | 'max';

const HANDLE_LABEL: Record<Handle, string> = { min: 'minimum', preferred: 'preferred', max: 'maximum (far right = unbounded)' };

function stepFor(scale: number): number {
  if (scale <= 500) return 1;
  if (scale <= 2000) return 5;
  return 10;
}

/**
 * Three native range inputs stacked on one track (min, preferred, max). Each is a real, labelled `input type=range`,
 * so pointer, touch and arrow keys all work. The max handle at the far right means unbounded. Faint ticks mark the defaults.
 */
export const KbpsSlider: React.FC<KbpsSliderProps> = ({ label, name, scale: baseScale, values, defaults, onChange }) => {
  const uid = useId();
  const [top, setTop] = useState<Handle>('preferred');
  const highest = Math.max(baseScale, values.min ?? 0, values.preferred ?? 0, values.max ?? 0, defaults.max ?? 0);
  const scale = highest;
  const step = stepFor(scale);

  const pos = {
    min: values.min ?? 0,
    preferred: values.preferred ?? values.min ?? 0,
    max: values.max !== null && values.max > 0 ? values.max : scale,
  };
  const pct = (v: number): number => (v / scale) * 100;

  const change = (handle: Handle, raw: number): void => {
    if (handle === 'min') {
      const cap = Math.min(values.preferred ?? Infinity, pos.max);
      onChange('min', String(Math.min(raw, cap)));
    } else if (handle === 'preferred') {
      onChange('preferred', String(Math.min(Math.max(raw, values.min ?? 0), pos.max)));
    } else {
      const floor = Math.max(values.min ?? 0, values.preferred ?? 0);
      const next = Math.max(raw, floor);
      onChange('max', next >= scale ? '' : String(next));
    }
  };

  const handles: Handle[] = ['min', 'preferred', 'max'];
  const defaultTicks = [defaults.min, defaults.preferred, defaults.max].filter((v): v is number => v !== null && v > 0);

  return (
    <div className="kbps-slider relative h-8" data-handle-top={top}>
      <div className="absolute inset-x-0 top-1/2 h-1.5 -translate-y-1/2 rounded-[2px] border border-[var(--border-subtle)] bg-[var(--bg-canvas)]" aria-hidden="true">
        <div
          className="absolute inset-y-0 rounded-[1px] bg-[var(--accent-amber)]/60"
          style={{ left: `${pct(pos.min)}%`, width: `${Math.max(0.5, pct(pos.max) - pct(pos.min))}%` }}
        />
        {defaultTicks.map((v) => (
          <span key={v} title={`Default ${v} kbps`} className="absolute -inset-y-1 w-px bg-white/30" style={{ left: `${pct(v)}%` }} />
        ))}
      </div>
      {handles.map((h) => (
        <input
          key={h}
          id={`${uid}-${h}`}
          name={`${name}-${h}-slider`}
          type="range"
          min={0}
          max={scale}
          step={step}
          value={pos[h]}
          aria-label={`${label} ${HANDLE_LABEL[h]} kbps`}
          aria-valuetext={h === 'max' && values.max === null ? 'unbounded' : `${pos[h]} kbps`}
          data-handle={h}
          onChange={(e) => change(h, Number(e.target.value))}
          onPointerDown={() => setTop(h)}
          onFocus={() => setTop(h)}
          className={`kbps-range absolute inset-0 ${top === h ? 'z-10' : 'z-0'}`}
        />
      ))}
    </div>
  );
};
