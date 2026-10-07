import React, { useCallback } from 'react';
import { Plus, Pencil, Trash2, Loader2, FlaskConical } from 'lucide-react';
import { PageActionsPortal } from '@/components/layout';
import { TapeDeckButton, MachinedCard, TactileSwitch, ActionBar, ConfirmDangerButton } from '@/components/ui';
import { useNotificationChannels } from '@/hooks/useNotificationChannels';
import { useNotificationChannelEditor } from '@/hooks/useNotificationChannelEditor';
import {
  NOTIFICATION_CHANNEL_TYPE_LABELS,
  NOTIFICATION_EVENT_LABELS,
  isNotificationChannelType,
} from '@/types/notifications';
import { NotificationChannelModal } from './NotificationChannelModal';

export interface NotificationsPanelProps {
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

export const NotificationsPanel: React.FC<NotificationsPanelProps> = ({ onToast }) => {
  const toast = useCallback((msg: string, tone: 'ok' | 'error' = 'ok') => onToast(msg, tone), [onToast]);
  const channels = useNotificationChannels(true, toast);
  const editor = useNotificationChannelEditor(channels.refresh, toast);

  return (
    <div className="space-y-4">
      <PageActionsPortal>
        <div className="flex items-center justify-end gap-2">
          <TapeDeckButton
            size="sm"
            variant="amber"
            aria-label="Add channel"
            title="Add channel"
            icon={<Plus className="h-3.5 w-3.5" />}
            collapseLabel
            onClick={editor.openNew}
          >
            Add Channel
          </TapeDeckButton>
        </div>
      </PageActionsPortal>
      <div className="text-xs font-mono text-neutral-400">
        Send request, download and issue events to Discord, Telegram, Pushover, email or a webhook.
      </div>

      {channels.loading && (
        <div className="flex justify-center py-12">
          <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
        </div>
      )}

      {!channels.loading && channels.channels.length === 0 && (
        <div className="text-center py-12 text-neutral-500 font-mono text-sm">No notification channels configured.</div>
      )}

      <div className="grid grid-cols-1 gap-3 content-start">
        {channels.channels.map((c) => {
          const events = c.events ?? [];
          return (
            <MachinedCard key={c.id} className="p-3 sm:p-4 space-y-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h4 className="font-bold text-sm sm:text-base text-white truncate" title={c.name}>
                    {c.name}
                  </h4>
                  <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] font-mono text-neutral-400">
                    <span className="px-2 py-0.5 rounded-[2px] bg-[#1a1a1a] border border-[#2a2a2a] uppercase text-neutral-300">
                      {isNotificationChannelType(c.channel_type) ? NOTIFICATION_CHANNEL_TYPE_LABELS[c.channel_type] : c.channel_type}
                    </span>
                    <span>{events.length} {events.length === 1 ? 'event' : 'events'}</span>
                  </div>
                </div>
                <TactileSwitch
                  checked={c.enabled}
                  onChange={(v) => void channels.setEnabled(c, v)}
                  title={c.enabled ? 'Disable channel' : 'Enable channel'}
                  ariaLabel={`Enable notification channel ${c.name}`}
                />
              </div>

              <div className="text-[11px] font-mono text-neutral-500 break-words">
                {events.length > 0 ? events.map((e) => NOTIFICATION_EVENT_LABELS[e] ?? e).join(', ') : 'No events selected'}
              </div>

              <ActionBar bay className="pt-1">
                <TapeDeckButton
                  size="sm"
                  disabled={channels.testingIds.has(c.id)}
                  onClick={() => void channels.test(c)}
                  icon={
                    channels.testingIds.has(c.id) ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <FlaskConical className="h-3.5 w-3.5" />
                    )
                  }
                >
                  Test
                </TapeDeckButton>
                <TapeDeckButton size="sm" onClick={() => editor.openEdit(c)} icon={<Pencil className="h-3.5 w-3.5" />}>
                  Edit
                </TapeDeckButton>
                <ConfirmDangerButton
                  idleLabel="Delete"
                  ariaLabel={`Delete ${c.name}`}
                  icon={<Trash2 className="h-3.5 w-3.5" />}
                  onConfirm={() => void channels.remove(c.id)}
                />
              </ActionBar>
            </MachinedCard>
          );
        })}
      </div>

      <NotificationChannelModal editor={editor} />
    </div>
  );
};
