import React from 'react';
import { PageActionsPortal } from '@/components/layout';
import { SourceBadge } from './SourceBadge';

export interface ListPanelProps {
  title: string;
  description?: string;
  mode: string | null;
  total: number;
  /** Pinned in the page frame's actions row (filters, bulk actions), kept to a single row. */
  toolbar?: React.ReactNode;
  children: React.ReactNode;
}

/** Header strip shared by the Activity and Wanted lists: title, count, source indicator, toolbar. */
export const ListPanel: React.FC<ListPanelProps> = ({ title, description, mode, total, toolbar, children }) => (
  <section className="space-y-3">
    {toolbar && (
      <PageActionsPortal>
        <div className="flex items-center justify-end gap-2 min-w-0">{toolbar}</div>
      </PageActionsPortal>
    )}
    <div className="min-w-0">
      <div className="min-w-0">
        <h4 className="text-sm font-bold uppercase font-mono text-white">
          {title}
          <span className="ml-2 text-neutral-500 font-normal">{total}</span>
        </h4>
        {description && <p className="text-xs text-neutral-400 font-mono mt-0.5">{description}</p>}
        <div className="mt-1">
          <SourceBadge mode={mode} />
        </div>
      </div>
    </div>
    {children}
  </section>
);
