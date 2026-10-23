import React from 'react';
import type { ActivitySeeding } from '@/types/activity';
import { formatMinutes } from '@/components/lists';

export const SeedingLine: React.FC<{ seeding: ActivitySeeding }> = ({ seeding }) => {
  const ratio =
    seeding.ratio_target == null ? seeding.ratio.toFixed(2) : `${seeding.ratio.toFixed(2)} / ${parseFloat(seeding.ratio_target.toFixed(2))}`;
  const time =
    seeding.time_target_minutes == null
      ? formatMinutes(seeding.seeding_minutes)
      : `${formatMinutes(seeding.seeding_minutes)} / ${formatMinutes(seeding.time_target_minutes)}`;
  const mins = seeding.removes_in_minutes;
  const removes =
    mins === null || mins === undefined || !seeding.action || seeding.action === 'keep'
      ? null
      : mins <= 0
        ? 'removes on next check'
        : `removes in ~${formatMinutes(mins)}`;
  return (
    <span className="basis-full text-[10px] font-mono text-[#e5a00d] break-words" title="Seeding ratio and time versus targets">
      Seeding &middot; {ratio} &middot; {time}
      {removes && <> &middot; {removes}</>}
    </span>
  );
};
