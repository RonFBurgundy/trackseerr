import React from 'react';
import { Loader2 } from 'lucide-react';
import { statusLabel, statusTone, type BadgeTone } from './taskFormat';

const TONE_CLASS: Record<BadgeTone, string> = {
  amber: 'bg-[#e5a00d]/10 text-[#e5a00d] border-[#e5a00d]/30',
  green: 'bg-green-950/40 text-[#22c55e] border-green-800/40',
  red: 'bg-red-950/40 text-[#ef4444] border-red-800/40',
  yellow: 'bg-yellow-950/40 text-yellow-400 border-yellow-800/40',
  neutral: 'bg-neutral-800/80 text-neutral-400 border-neutral-700',
};

export interface TaskStatusBadgeProps {
  status: string | null | undefined;
  className?: string;
}

/** Status / run-result chip. A running status gets the spinner and a slow amber pulse (motion-safe only). */
export const TaskStatusBadge: React.FC<TaskStatusBadgeProps> = React.memo(({ status, className = '' }) => {
  const tone = statusTone(status);
  const running = tone === 'amber';
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[3px] border font-mono font-bold text-[10px] uppercase tracking-wide ${TONE_CLASS[tone]} ${running ? 'motion-safe:animate-pulse' : ''} ${className}`}
    >
      {running && <Loader2 className="h-3 w-3 motion-safe:animate-spin" aria-hidden="true" />}
      {statusLabel(status)}
    </span>
  );
});
TaskStatusBadge.displayName = 'TaskStatusBadge';
