import React from 'react';

export interface ProgressMeterProps {
  /** 0-1 fraction. */
  value: number;
  tone?: 'default' | 'warning';
}

/** Recessed tape-counter bar with an amber fill. */
export const ProgressMeter: React.FC<ProgressMeterProps> = ({ value, tone = 'default' }) => {
  const pct = Math.max(0, Math.min(100, Math.round((Number.isFinite(value) ? value : 0) * 100)));
  const fill = tone === 'warning' ? 'bg-red-500' : 'bg-[#e5a00d] shadow-[0_0_6px_rgba(229,160,13,0.5)]';
  return (
    <div className="flex items-center gap-2 w-full min-w-0">
      <div
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        className="relative h-2 flex-1 min-w-[48px] bg-[#0d0d0d] border border-[#1f1f1f] rounded-[2px] shadow-[inset_0_1px_2px_rgba(0,0,0,0.8)] overflow-hidden"
      >
        <div className={`h-full ${fill}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-[10px] font-mono text-neutral-400 w-8 text-right tabular-nums">{pct}%</span>
    </div>
  );
};
