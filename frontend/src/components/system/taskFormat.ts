import type { ScheduledTaskItem } from '@/types/models';

export type BadgeTone = 'amber' | 'green' | 'red' | 'yellow' | 'neutral';

const EM_DASH = '—';

/** 300 -> "5m", 3600 -> "1h", 86400 -> "24h", 604800 -> "7d". */
export function formatIntervalSeconds(seconds: number): string {
  if (seconds >= 2 * 86400 && seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

/** Schedule cell text for tasks without an editable select. */
export function scheduleLabel(task: ScheduledTaskItem): string {
  if (task.schedule_kind === 'continuous') return 'Continuous';
  if (task.schedule_kind === 'manual') return 'Manual';
  if (task.interval_seconds != null) return `Every ${formatIntervalSeconds(task.interval_seconds)}`;
  return task.interval;
}

/** "3m 05s" style elapsed time; negative or invalid input renders as 0s. */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

/** Countdown to a future instant; "Due" once reached. */
export function formatCountdown(msUntil: number): string {
  if (msUntil <= 0) return 'Due';
  const total = Math.ceil(msUntil / 1000);
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

export function formatRss(bytes: number | null | undefined): string {
  if (bytes == null) return EM_DASH;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? value.toFixed(0) : value.toFixed(value >= 100 ? 0 : 1)} ${units[unit]}`;
}

export function formatUptime(seconds: number | null | undefined): string {
  if (seconds == null) return EM_DASH;
  const total = Math.max(0, Math.floor(seconds));
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export function formatPercent(value: number | null | undefined): string {
  return value == null ? EM_DASH : `${value.toFixed(value >= 10 ? 0 : 1)}%`;
}

export function formatCount(value: number | null | undefined): string {
  return value == null ? EM_DASH : String(value);
}

/** Map a task or run status string to a badge tone; unknown values stay neutral. */
export function statusTone(status: string | null | undefined): BadgeTone {
  switch ((status ?? '').toLowerCase()) {
    case 'running':
      return 'amber';
    case 'success':
    case 'succeeded':
    case 'ok':
    case 'completed':
      return 'green';
    case 'failed':
    case 'error':
      return 'red';
    case 'paused':
    case 'cancelled':
    case 'canceled':
      return 'yellow';
    default:
      return 'neutral';
  }
}

export function statusLabel(status: string | null | undefined): string {
  return status ? status.replace(/_/g, ' ') : EM_DASH;
}

export interface TaskGroup {
  key: string;
  label: string;
  tasks: ScheduledTaskItem[];
  runningCount?: number;
}

export interface GroupedTasks {
  running: ScheduledTaskItem[];
  groups: TaskGroup[];
}

/**
 * Filter tasks by name or description (case-insensitive) and group them.
 * Running tasks are segregated into `running` and not repeated in group tasks.
 * Groups appear in order of first appearance from the tasks list.
 */
export function groupTasks(tasks: ScheduledTaskItem[], query?: string): GroupedTasks {
  const q = query?.trim().toLowerCase() ?? '';

  const matches = (task: ScheduledTaskItem): boolean => {
    if (!q) return true;
    const nameMatch = task.name.toLowerCase().includes(q);
    const descMatch = (task.description ?? '').toLowerCase().includes(q);
    return nameMatch || descMatch;
  };

  const filtered = q ? tasks.filter(matches) : tasks;

  const running: ScheduledTaskItem[] = [];
  const groupMap = new Map<string, TaskGroup>();

  for (const task of filtered) {
    if (task.status === 'running') {
      running.push(task);
    }

    const groupKey = task.group || 'other';
    const groupLabel = task.group_label || task.group || 'Other';

    let group = groupMap.get(groupKey);
    if (!group) {
      group = {
        key: groupKey,
        label: groupLabel,
        tasks: [],
        runningCount: 0,
      };
      groupMap.set(groupKey, group);
    }

    if (task.status === 'running') {
      group.runningCount = (group.runningCount ?? 0) + 1;
    } else {
      group.tasks.push(task);
    }
  }

  // Only retain groups that have matching tasks
  const groups = Array.from(groupMap.values()).filter((g) => g.tasks.length > 0);

  return { running, groups };
}
