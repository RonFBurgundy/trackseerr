import React, { useId } from 'react';
import { ChevronDown } from 'lucide-react';
import { OverflowMenu, SelectionCheckbox, type OverflowMenuItem } from '@/components/ui';
import type { FlatListColumn, MobileCardLayout, RowTone } from './FlatList';
import { TONE_CLASS } from './FlatList';

export interface ItemCardProps<T> {
  item: T;
  columns?: MobileCardLayout<T>;
  title?: FlatListColumn<T>;
  sub?: ReadonlyArray<FlatListColumn<T>>;
  meta?: ReadonlyArray<FlatListColumn<T>>;
  end?: ReadonlyArray<FlatListColumn<T>>;
  menu?: OverflowMenuItem[];
  menuLabel: string;
  monitor?: React.ReactNode;
  details?: React.ReactNode | null;
  attention?: { label: string; onSelect: () => void } | null;
  tone?: RowTone;
  selectable?: boolean;
  selected?: boolean;
  onToggle?: () => void;
  expanded?: boolean;
  onToggleExpand?: () => void;
  className?: string;
}

function ItemCardInner<T>({
  item,
  columns,
  title,
  sub,
  meta,
  end,
  menu,
  menuLabel,
  monitor,
  details,
  attention,
  tone,
  selectable = false,
  selected = false,
  onToggle,
  expanded = false,
  onToggleExpand,
  className = '',
}: ItemCardProps<T>): React.ReactElement | null {
  const detailsId = useId();

  const split: MobileCardLayout<T> | null = columns ?? (title ? {
    title,
    sub: sub ?? [],
    meta: meta ?? [],
    end: end ?? [],
  } : null);

  if (!split) return null;

  return (
    <div
      role="row"
      aria-selected={selectable ? selected : undefined}
      className={`flex items-start gap-2.5 border-b border-[#1c1c1c] border-l-2 hover:bg-[#1a1a1a] px-3 py-2 sm:py-1.5 ${
        tone ? TONE_CLASS[tone] : 'border-l-transparent'
      } ${selected ? 'bg-[#e5a00d]/5' : 'bg-[#141414]'} ${className}`}
    >
      {selectable && (
        <div role="cell" className="shrink-0 flex items-center pt-0.5">
          <SelectionCheckbox
            inline
            checked={selected}
            label="Select row"
            onChange={onToggle ?? (() => {})}
          />
        </div>
      )}

      <div role="cell" className="min-w-0 flex-1 flex flex-col gap-1.5 font-mono">
        {/* Row 1: details (title · artist · album, truncating) + monitor + chevron + overflow menu */}
        <div className="flex items-start sm:items-center justify-between gap-2 min-w-0">
          <div className="min-w-0 flex-1">
            {/* ≥sm (desktop): title and sub on ONE truncating line */}
            <div className="hidden sm:flex items-center gap-1.5 min-w-0 text-sm leading-5">
              <div className="font-bold text-white truncate min-w-0">
                {split.title.render(item)}
              </div>
              {split.sub.length > 0 && (
                <div className="flex items-center gap-1.5 min-w-0 text-xs text-neutral-400 truncate">
                  <span aria-hidden="true" className="shrink-0 text-neutral-600">&middot;</span>
                  {split.sub.map((col, idx) => (
                    <React.Fragment key={col.key}>
                      {idx > 0 && <span aria-hidden="true" className="shrink-0 text-neutral-600">&middot;</span>}
                      <span className="truncate">{col.render(item)}</span>
                    </React.Fragment>
                  ))}
                </div>
              )}
            </div>

            {/* <sm (mobile): title bold, wrapped to a second muted line for sub */}
            <div className="sm:hidden min-w-0">
              <div className="font-bold text-white truncate text-sm leading-5">
                {split.title.render(item)}
              </div>
              {split.sub.length > 0 && (
                <div className="flex items-center gap-1.5 min-w-0 text-xs leading-4 text-neutral-400 truncate mt-0.5">
                  {split.sub.map((col, idx) => (
                    <React.Fragment key={col.key}>
                      {idx > 0 && <span aria-hidden="true" className="shrink-0 text-neutral-600">&middot;</span>}
                      <span className="truncate">{col.render(item)}</span>
                    </React.Fragment>
                  ))}
                </div>
              )}
            </div>
          </div>

          <div className="flex items-center gap-1 shrink-0 -my-1">
            {monitor}
            {details != null && (
              <button
                type="button"
                aria-label={expanded ? 'Hide details' : 'Show details'}
                aria-expanded={expanded}
                aria-controls={detailsId}
                onClick={onToggleExpand}
                className="inline-flex h-9 w-9 items-center justify-center rounded-[3px] text-neutral-400 hover:text-white focus:outline-none focus-visible:ring-1 focus-visible:ring-[#e5a00d]"
              >
                <ChevronDown className={`h-4 w-4 transition-transform duration-150 ${expanded ? 'rotate-180' : ''}`} />
              </button>
            )}
            {menu && menu.length > 0 && (
              <OverflowMenu items={menu} label={menuLabel} attention={!!attention} />
            )}
          </div>
        </div>

        {/* Row 2: end + meta columns in a wrapping flex row */}
        {(split.end.length > 0 || split.meta.length > 0) && (
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {split.end.length > 0 && (
              attention ? (
                <button
                  type="button"
                  onClick={attention.onSelect}
                  aria-label={attention.label}
                  className="inline-flex items-center gap-1.5 rounded-[3px] text-left focus:outline-none focus-visible:ring-1 focus-visible:ring-[#e5a00d] hover:opacity-90"
                >
                  {split.end.map((c) => (
                    <span key={c.key} className="min-w-0">
                      {c.render(item)}
                    </span>
                  ))}
                </button>
              ) : (
                <div className="inline-flex items-center gap-1.5 min-w-0">
                  {split.end.map((c) => (
                    <span key={c.key} className="min-w-0">
                      {c.render(item)}
                    </span>
                  ))}
                </div>
              )
            )}
            {split.meta.map((c) => (
              <span key={c.key} className="min-w-0 text-neutral-400">
                {c.render(item)}
              </span>
            ))}
          </div>
        )}

        {/* Expanded details */}
        {expanded && details != null && (
          <div id={detailsId} className="pt-1 text-[11px] font-mono text-neutral-400">
            {details}
          </div>
        )}
      </div>
    </div>
  );
}

export const ItemCard = React.memo(ItemCardInner) as typeof ItemCardInner;
