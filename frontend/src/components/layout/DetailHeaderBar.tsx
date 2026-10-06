import React from 'react';
import { ArrowLeft } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';

export interface DetailHeaderBarProps {
  /** Name of the list this page drills out of ("Artists", "Collections"); shown beside the arrow from `sm` up. */
  parentLabel: string;
  /** Page title (artist or collection name); truncates. */
  title?: string;
  onBack: () => void;
  /** Optional trailing keys. */
  actions?: React.ReactNode;
}

/** Pinned header row of a drill-down page: back key, parent label and the truncated title, in a recessed bay. */
export const DetailHeaderBar: React.FC<DetailHeaderBarProps> = ({ parentLabel, title, onBack, actions }) => (
  <div className="tape-transport-bay flex min-w-0 items-center gap-2 rounded-[4px] border border-[#1f1f1f] bg-[#0d0d0d] p-1 shadow-transport-bay">
    <TapeDeckButton
      size="sm"
      className="shrink-0 after:!-inset-y-[4px]"
      onClick={onBack}
      icon={<ArrowLeft className="h-4 w-4" />}
      collapseLabel="sm"
      aria-label={`Back to ${parentLabel}`}
      title={`Back to ${parentLabel}`}
    >
      {parentLabel}
    </TapeDeckButton>
    <h2 className="min-w-0 flex-1 truncate font-mono text-sm font-bold text-white" title={title}>
      {title}
    </h2>
    {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
  </div>
);
