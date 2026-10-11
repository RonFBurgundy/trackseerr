import React from 'react';
import type { ActivityQueueRecord } from '@/types/activity';

/** Muted peer / folder / remote-state line shown under a Soulseek queue row's title. */
export const SoulseekQueueDetail: React.FC<{ record: ActivityQueueRecord }> = ({ record }) => {
  if (!record.peer) return null;
  const text = `Soulseek \u00b7 ${record.peer}${record.is_folder ? ' \u00b7 folder' : ''}${
    record.remote_state ? ` \u00b7 ${record.remote_state}` : ''
  }`;
  return (
    <span className="truncate text-[10px] font-mono text-neutral-500" title={text}>
      {text}
    </span>
  );
};
