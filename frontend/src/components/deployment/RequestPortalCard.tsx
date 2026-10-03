import React from 'react';
import { AlertTriangle } from 'lucide-react';
import { CopyBox, MachinedCard } from '@/components/ui';
import { useGatewayStatus } from '@/hooks/useGatewayStatus';
import type { GatewayState } from '@/types/deployment';

const STATE_STYLE: Record<GatewayState, { label: string; cls: string }> = {
  online: {
    label: 'Online',
    cls: 'text-[var(--status-success)] border-[var(--status-success)]',
  },
  stale: {
    label: 'Stale',
    cls: 'text-[var(--accent-amber)] border-[var(--accent-amber)]',
  },
  never_seen: {
    label: 'Never seen',
    cls: 'text-[var(--text-muted)] border-[var(--border-default)]',
  },
  not_used: {
    label: 'Not used',
    cls: 'text-[var(--text-muted)] border-[var(--border-default)]',
  },
};

export function formatRelativeTime(iso: string | null, now: number = Date.now()): string {
  if (!iso) return 'Never';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return 'Unknown';
  const secs = Math.max(0, Math.round((now - then) / 1000));
  if (secs < 10) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export const RequestPortalCard: React.FC = () => {
  const { status, error } = useGatewayStatus(true);

  if (!status) {
    return error ? (
      <MachinedCard className="p-6 max-w-2xl">
        <p className="text-xs font-mono text-[var(--status-error)]">{error}</p>
      </MachinedCard>
    ) : null;
  }
  if (status.state === 'not_used') return null;

  const style = STATE_STYLE[status.state];

  return (
    <MachinedCard className="p-6 max-w-2xl space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h4 className="text-sm font-bold uppercase font-mono text-white">Request portal</h4>
        <span
          className={`px-2 py-0.5 text-[10px] font-mono font-bold uppercase border rounded-[3px] ${style.cls}`}
        >
          {style.label}
        </span>
      </div>

      {status.version_match === false && (
        <div
          role="alert"
          className="flex items-start gap-2 p-3 bg-[var(--bg-surface)] border border-[var(--accent-amber)] text-xs font-mono text-[var(--accent-amber)] rounded-[4px]"
        >
          <AlertTriangle className="h-4 w-4 flex-shrink-0" />
          <span>
            Request portal version{status.version ? ` ${status.version}` : ''} differs from this
            core. Run the same TrackSeerr version on both containers.
          </span>
        </div>
      )}

      {status.public_url && <CopyBox label="Public URL" value={status.public_url} />}

      <div className="divide-y divide-[var(--border-subtle)] text-xs font-mono">
        <div className="py-2.5 flex justify-between gap-3">
          <span className="text-[var(--text-secondary)]">Last seen:</span>
          <span className="text-white" title={status.last_seen_at ?? undefined}>
            {formatRelativeTime(status.last_seen_at)}
          </span>
        </div>
        <div className="py-2.5 flex justify-between gap-3">
          <span className="text-[var(--text-secondary)]">Version:</span>
          <span className="text-white">{status.version ?? 'Unknown'}</span>
        </div>
        <div className="py-2.5 flex justify-between gap-3">
          <span className="text-[var(--text-secondary)]">Active sessions:</span>
          <span className="text-white">{status.active_sessions ?? '-'}</span>
        </div>
      </div>
    </MachinedCard>
  );
};
