import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Loader2, RotateCw, AlertTriangle } from 'lucide-react';
import { TapeDeckButton, MachinedCard, SearchBar } from '@/components/ui';
import { PageActionsPortal } from '@/components/layout';
import { useTaskManager } from '@/hooks/useTaskManager';
import { useNow } from '@/hooks/useNow';
import { TaskResourceStrip } from './TaskResourceStrip';
import { TaskRow } from './TaskRow';
import { TaskGroupSection } from './TaskGroupSection';
import { groupTasks } from './taskFormat';

export interface SystemTasksPanelProps {
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Settings > System > Tasks: process gauge plus grouped live tasks. */
export const SystemTasksPanel: React.FC<SystemTasksPanelProps> = ({ onToast }) => {
  const {
    tasks,
    isLoading,
    isRefreshing,
    error,
    runningIds,
    cancellingIds,
    savingScheduleIds,
    refresh,
    run,
    cancel,
    setSchedule,
  } = useTaskManager(onToast);

  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [openGroupKeys, setOpenGroupKeys] = useState<Set<string>>(() => new Set());
  const [collapsedInSearch, setCollapsedInSearch] = useState<Set<string>>(() => new Set());
  const initializedRef = useRef(false);

  // Groups start collapsed except that any group containing a task with last_run_status === 'failed' starts open
  useEffect(() => {
    if (!initializedRef.current && tasks.length > 0) {
      const initial = new Set<string>();
      for (const t of tasks) {
        if (t.last_run_status === 'failed' && t.group) {
          initial.add(t.group);
        }
      }
      setOpenGroupKeys(initial);
      initializedRef.current = true;
    }
  }, [tasks]);

  // When search query changes, reset search-specific collapses
  useEffect(() => {
    setCollapsedInSearch(new Set());
  }, [query]);

  const isFiltering = query.trim().length > 0;
  const { running, groups } = useMemo(() => groupTasks(tasks, query), [tasks, query]);
  const totalMatches = running.length + groups.reduce((acc, g) => acc + g.tasks.length, 0);

  const needsClock = tasks.some((t) => t.status === 'running' || t.schedule_kind === 'interval');
  const now = useNow(1000, needsClock);

  const toggleExpanded = useCallback((taskId: string) => setExpandedId((cur) => (cur === taskId ? null : taskId)), []);
  const handleRun = useCallback((taskId: string) => void run(taskId), [run]);
  const handleCancel = useCallback((taskId: string) => void cancel(taskId), [cancel]);
  const handleSchedule = useCallback((taskId: string, seconds: number | null) => void setSchedule(taskId, seconds), [setSchedule]);

  const isGroupOpen = useCallback(
    (groupKey: string) => {
      if (isFiltering) {
        return !collapsedInSearch.has(groupKey);
      }
      return openGroupKeys.has(groupKey);
    },
    [isFiltering, collapsedInSearch, openGroupKeys]
  );

  const toggleGroup = useCallback(
    (groupKey: string) => {
      if (isFiltering) {
        setCollapsedInSearch((prev) => {
          const next = new Set(prev);
          if (next.has(groupKey)) next.delete(groupKey);
          else next.add(groupKey);
          return next;
        });
      } else {
        setOpenGroupKeys((prev) => {
          const next = new Set(prev);
          if (next.has(groupKey)) next.delete(groupKey);
          else next.add(groupKey);
          return next;
        });
      }
    },
    [isFiltering]
  );

  return (
    <div className="space-y-4 min-w-0">
      <PageActionsPortal>
        <div className="flex items-center justify-end gap-2">
          <TapeDeckButton
            size="sm"
            onClick={() => void refresh()}
            disabled={isRefreshing}
            collapseLabel
            aria-label="Refresh tasks"
            title="Refresh tasks"
            icon={isRefreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCw className="h-3.5 w-3.5" />}
          >
            Refresh
          </TapeDeckButton>
        </div>
      </PageActionsPortal>

      <TaskResourceStrip />

      <div className="min-w-0">
        <h4 className="text-sm font-bold uppercase font-mono text-white">Scheduled Tasks &amp; Background Workers</h4>
        <p className="text-xs text-neutral-400 font-mono mt-0.5">
          Live status, next run, schedules and 7-day run history. Trigger on-demand sweeps.
        </p>
      </div>

      <SearchBar
        id="task-search"
        name="task-search"
        ariaLabel="Filter tasks"
        placeholder="Filter tasks by name or description..."
        value={query}
        onChange={setQuery}
      />

      {error && (
        <div role="alert" className="p-3 bg-red-950/40 border border-red-800/50 rounded-[4px] text-xs text-red-300 font-mono flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="min-w-0 break-words">{error}</span>
        </div>
      )}

      <MachinedCard className="overflow-hidden p-0 border-[#222222] divide-y divide-[#222222]">
        {tasks.length > 0 && (
          <>
            {running.length > 0 && (
              <TaskGroupSection
                id="running-now"
                label="Running now"
                count={running.length}
                collapsible={false}
              >
                {running.map((task) => (
                  <TaskRow
                    key={task.id}
                    task={task}
                    now={now}
                    isTriggering={runningIds.has(task.id)}
                    isCancelling={cancellingIds.has(task.id)}
                    isSavingSchedule={savingScheduleIds.has(task.id)}
                    expanded={expandedId === task.id}
                    onToggleExpanded={toggleExpanded}
                    onRun={handleRun}
                    onCancel={handleCancel}
                    onSchedule={handleSchedule}
                  />
                ))}
              </TaskGroupSection>
            )}

            {groups.map((group) => (
              <TaskGroupSection
                key={group.key}
                id={group.key}
                label={group.label}
                count={group.tasks.length + (group.runningCount ?? 0)}
                runningCount={group.runningCount}
                open={isGroupOpen(group.key)}
                onToggle={() => toggleGroup(group.key)}
              >
                {group.tasks.map((task) => (
                  <TaskRow
                    key={task.id}
                    task={task}
                    now={now}
                    isTriggering={runningIds.has(task.id)}
                    isCancelling={cancellingIds.has(task.id)}
                    isSavingSchedule={savingScheduleIds.has(task.id)}
                    expanded={expandedId === task.id}
                    onToggleExpanded={toggleExpanded}
                    onRun={handleRun}
                    onCancel={handleCancel}
                    onSchedule={handleSchedule}
                  />
                ))}
              </TaskGroupSection>
            ))}

            {isFiltering && totalMatches === 0 && (
              <div className="text-center py-12 text-neutral-500 font-mono text-sm">
                No tasks match
              </div>
            )}
          </>
        )}

        {isLoading && tasks.length === 0 && (
          <div className="flex items-center justify-center gap-2 py-12 text-neutral-400 text-xs font-mono">
            <Loader2 className="h-4 w-4 animate-spin text-[#e5a00d]" aria-hidden="true" /> Loading scheduled tasks...
          </div>
        )}

        {!isLoading && tasks.length === 0 && (
          <div className="text-center py-12 text-neutral-500 font-mono text-sm">
            {error ? 'Scheduled tasks could not be loaded.' : 'No scheduled background tasks registered.'}
          </div>
        )}
      </MachinedCard>
    </div>
  );
};
