import React from 'react';
import { SourceBadge } from './SourceBadge';

export interface ListPanelProps {
  title: string;
  description?: string;
  mode: string | null;
  total: number;
  /** Right-aligned toolbar (filters, bulk actions). */
  toolbar?: React.ReactNode;
  children: React.ReactNode;
}

/** Header strip shared by the Activity and Wanted lists: title, count, source indicator, toolbar. */
export const ListPanel: React.FC<ListPanelProps> = ({ title, description, mode, total, toolbar, children }) => (
  <section className="space-y-3">
    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
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
      {toolbar && <div className="flex flex-wrap items-center gap-2">{toolbar}</div>}
    </div>
    {children}
  </section>
);
