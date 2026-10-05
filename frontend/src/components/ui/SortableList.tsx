import React, { useCallback, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, GripVertical } from 'lucide-react';
import { TapeDeckButton } from './TapeDeckButton';

/** What `renderItem` receives for one row. Pass it to `SortControls` (or wire the pieces by hand). */
export interface SortableRowState {
  index: number;
  count: number;
  dragging: boolean;
  moveUp: () => void;
  moveDown: () => void;
  handleProps: Pick<
    React.HTMLAttributes<HTMLButtonElement>,
    'onPointerDown' | 'onPointerMove' | 'onPointerUp' | 'onPointerCancel' | 'onKeyDown'
  >;
}

export interface SortableListProps<T> {
  items: readonly T[];
  getKey: (item: T) => string;
  onReorder: (next: T[]) => void;
  renderItem: (item: T, state: SortableRowState) => React.ReactNode;
  ariaLabel: string;
  className?: string;
}

function moved<T>(list: readonly T[], from: number, to: number): T[] {
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

/**
 * Reorderable list. Pointer drag (mouse, touch, pen) works from the grip handle; the same handle also answers
 * ArrowUp/ArrowDown, and `SortControls` adds explicit up/down keys, so ordering never requires dragging.
 */
export function SortableList<T>({ items, getKey, onReorder, renderItem, ariaLabel, className = '' }: SortableListProps<T>): React.ReactElement {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [drag, setDrag] = useState<{ from: number; over: number } | null>(null);

  const rowAt = useCallback((clientY: number, count: number): number => {
    const rows = containerRef.current?.querySelectorAll<HTMLElement>(':scope > [data-sortable-row]');
    if (!rows) return 0;
    for (let i = 0; i < Math.min(rows.length, count); i += 1) {
      const rect = rows[i].getBoundingClientRect();
      if (clientY < rect.top + rect.height / 2) return i;
    }
    return count - 1;
  }, []);

  const shift = (from: number, delta: number): void => {
    const to = from + delta;
    if (to < 0 || to >= items.length) return;
    onReorder(moved(items, from, to));
  };

  return (
    <div ref={containerRef} role="list" aria-label={ariaLabel} className={className}>
      {items.map((item, index) => {
        const dragging = drag?.from === index;
        const target = drag !== null && drag.over === index && drag.from !== index;
        const edge = target && drag !== null ? (drag.over < drag.from ? 'shadow-[inset_0_2px_0_var(--accent-amber)]' : 'shadow-[inset_0_-2px_0_var(--accent-amber)]') : '';
        const state: SortableRowState = {
          index,
          count: items.length,
          dragging,
          moveUp: () => shift(index, -1),
          moveDown: () => shift(index, 1),
          handleProps: {
            onPointerDown: (e) => {
              if (e.pointerType === 'mouse' && e.button !== 0) return;
              e.currentTarget.setPointerCapture(e.pointerId);
              setDrag({ from: index, over: index });
            },
            onPointerMove: (e) => {
              if (drag === null || drag.from !== index) return;
              const over = rowAt(e.clientY, items.length);
              if (over !== drag.over) setDrag({ from: index, over });
            },
            onPointerUp: (e) => {
              if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
              if (drag !== null && drag.over !== drag.from) onReorder(moved(items, drag.from, drag.over));
              setDrag(null);
            },
            onPointerCancel: () => setDrag(null),
            onKeyDown: (e) => {
              if (e.key === 'ArrowUp') {
                e.preventDefault();
                shift(index, -1);
              } else if (e.key === 'ArrowDown') {
                e.preventDefault();
                shift(index, 1);
              }
            },
          },
        };
        return (
          <div key={getKey(item)} role="listitem" data-sortable-row className={`${dragging ? 'opacity-60' : ''} ${edge}`}>
            {renderItem(item, state)}
          </div>
        );
      })}
    </div>
  );
}

export interface SortControlsProps {
  state: SortableRowState;
  /** Names the row for assistive tech, e.g. "FLAC 24bit". */
  label: string;
}

/** Grip handle (drag, or ArrowUp/ArrowDown) plus explicit up/down keys. */
export const SortControls: React.FC<SortControlsProps> = ({ state, label }) => (
  <div className="flex items-center gap-1 shrink-0">
    <button
      type="button"
      aria-label={`Reorder ${label} (drag, or use the arrow keys)`}
      title="Drag to reorder"
      className="inline-flex h-9 w-7 cursor-grab touch-none items-center justify-center rounded-[3px] text-neutral-500 hover:text-[var(--accent-amber)] focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--accent-amber)] active:cursor-grabbing"
      {...state.handleProps}
    >
      <GripVertical className="h-4 w-4" />
    </button>
    <TapeDeckButton
      size="sm"
      aria-label={`Move ${label} up`}
      disabled={state.index === 0}
      onClick={state.moveUp}
      icon={<ArrowUp className="h-3.5 w-3.5" />}
    />
    <TapeDeckButton
      size="sm"
      aria-label={`Move ${label} down`}
      disabled={state.index === state.count - 1}
      onClick={state.moveDown}
      icon={<ArrowDown className="h-3.5 w-3.5" />}
    />
  </div>
);
