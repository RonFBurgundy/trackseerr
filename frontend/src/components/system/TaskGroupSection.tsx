import React, { useId } from 'react';
import { ChevronDown } from 'lucide-react';

export interface TaskGroupSectionProps {
  id?: string;
  label: string;
  count: number;
  runningCount?: number;
  open?: boolean;
  onToggle?: () => void;
  collapsible?: boolean;
  children: React.ReactNode;
}

export const TaskGroupSection: React.FC<TaskGroupSectionProps> = ({
  id,
  label,
  count,
  runningCount,
  open = true,
  onToggle,
  collapsible,
  children,
}) => {
  const generatedId = useId();
  const bodyId = id ? `task-group-body-${id}` : `task-group-body-${generatedId}`;
  const isCollapsible = collapsible ?? (onToggle !== undefined);
  const isOpen = isCollapsible ? open : true;
  const countLabel = count === 1 ? '1 task' : `${count} tasks`;

  return (
    <section className="min-w-0" aria-label={label}>
      {isCollapsible ? (
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={isOpen}
          aria-controls={bodyId}
          className={`w-full flex items-center justify-between min-h-[44px] px-3 sm:px-4 py-2.5 bg-[#121212] hover:bg-[#181818] transition-colors duration-75 text-left ${
            isOpen ? 'border-b border-[#1c1c1c]' : ''
          } focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--accent-amber)]`}
        >
          <div className="flex items-center gap-2 min-w-0">
            <span className="font-mono font-bold text-xs sm:text-sm uppercase tracking-wider text-white truncate">
              {label}
            </span>
            <span className="text-xs font-mono text-neutral-400 shrink-0">
              {countLabel}
              {runningCount != null && runningCount > 0 && (
                <span className="text-[#e5a00d]"> · {runningCount} running</span>
              )}
            </span>
          </div>
          <ChevronDown
            className={`h-4 w-4 text-neutral-400 transition-transform duration-150 shrink-0 ml-2 ${
              isOpen ? 'rotate-180' : ''
            }`}
            aria-hidden="true"
          />
        </button>
      ) : (
        <div className="w-full flex items-center justify-between min-h-[44px] px-3 sm:px-4 py-2.5 bg-[#121212] border-b border-[#1c1c1c]">
          <div className="flex items-center gap-2 min-w-0">
            <span className="font-mono font-bold text-xs sm:text-sm uppercase tracking-wider text-white truncate">
              {label}
            </span>
            <span className="text-xs font-mono text-neutral-400 shrink-0">
              {countLabel}
              {runningCount != null && runningCount > 0 && (
                <span className="text-[#e5a00d]"> · {runningCount} running</span>
              )}
            </span>
          </div>
        </div>
      )}

      {isOpen && (
        <ul id={bodyId} className="divide-y divide-[#1c1c1c]">
          {children}
        </ul>
      )}
    </section>
  );
};
