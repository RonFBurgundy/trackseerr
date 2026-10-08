import React, { useCallback } from 'react';
import {
  Bell,
  FlaskConical,
  Loader2,
  Pencil,
  Plus,
  Trash2,
  CheckCircle,
  AlertTriangle,
} from 'lucide-react';
import {
  MachinedCard,
  TapeDeckButton,
  TactileSwitch,
  ActionBar,
  ConfirmDangerButton,
  CassetteLoader,
} from '@/components/ui';
import { useUserWebPush } from '@/hooks/useUserWebPush';
import { useUserNotificationPrefs } from '@/hooks/useUserNotificationPrefs';
import { useUserChannels } from '@/hooks/useUserChannels';
import { useUserChannelEditor } from '@/hooks/useUserChannelEditor';
import { USER_NOTIFICATION_EVENTS } from '@/types/userNotifications';
import {
  NOTIFICATION_CHANNEL_TYPE_LABELS,
  NOTIFICATION_EVENT_LABELS,
} from '@/types/notifications';
import { UserChannelModal } from '@/components/notifications/UserChannelModal';

export interface UserNotificationsSectionProps {
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

export const UserNotificationsSection: React.FC<UserNotificationsSectionProps> = ({
  onToast,
}) => {
  const toast = useCallback(
    (msg: string, tone: 'ok' | 'error' = 'ok') => onToast(msg, tone),
    [onToast]
  );

  const push = useUserWebPush(true, toast);
  const prefsHook = useUserNotificationPrefs(true, toast);
  const channels = useUserChannels(true, toast);
  const editor = useUserChannelEditor(channels.refresh, toast);

  return (
    <div className="space-y-4">
      {/* 1. Device Web Push Section */}
      <MachinedCard className="p-3 sm:p-6 space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-bold uppercase tracking-wider text-white">
              Browser Web Push
            </h3>
            <p className="text-xs font-mono text-[var(--text-secondary)] mt-0.5">
              Receive browser push alerts on this device even when TrackSeerr is closed.
            </p>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            {push.isSubscribed ? (
              <>
                <TapeDeckButton
                  size="sm"
                  variant="default"
                  disabled={push.testing}
                  onClick={() => void push.sendTest()}
                  icon={
                    push.testing ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <FlaskConical className="h-3.5 w-3.5" />
                    )
                  }
                >
                  Send test
                </TapeDeckButton>
                <TapeDeckButton
                  size="sm"
                  variant="default"
                  disabled={push.loading}
                  onClick={() => void push.unsubscribe()}
                >
                  {push.loading ? 'Updating...' : 'Disable on this device'}
                </TapeDeckButton>
              </>
            ) : push.isSupported && push.isSecureContext && push.permission !== 'denied' ? (
              <TapeDeckButton
                size="sm"
                variant="amber"
                disabled={push.loading}
                onClick={() => void push.subscribe()}
                icon={
                  push.loading ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Bell className="h-3.5 w-3.5" />
                  )
                }
              >
                {push.loading ? 'Enabling...' : 'Enable push on this device'}
              </TapeDeckButton>
            ) : null}
          </div>
        </div>

        {/* Web Push Status Explanations */}
        {!push.isSecureContext ? (
          <div className="flex items-start gap-2 p-3 rounded-[3px] bg-[#1a1510] border border-[#4a2e10] text-xs font-mono text-amber-200">
            <AlertTriangle className="h-4 w-4 text-amber-400 shrink-0 mt-0.5" />
            <div>
              <p className="font-bold">HTTPS Connection Required</p>
              <p className="text-neutral-400 mt-0.5">
                Web Push notifications require a secure context (HTTPS or localhost). This device is
                connected over plain HTTP, so browser push is unavailable.
              </p>
            </div>
          </div>
        ) : !push.isSupported ? (
          <div className="flex items-start gap-2 p-3 rounded-[3px] bg-[#181818] border border-[#2a2a2a] text-xs font-mono text-neutral-400">
            <Bell className="h-4 w-4 text-neutral-500 shrink-0 mt-0.5" />
            <span>
              Web Push notifications are not supported by this browser or platform.
            </span>
          </div>
        ) : push.permission === 'denied' ? (
          <div className="flex items-start gap-2 p-3 rounded-[3px] bg-[#201010] border border-[#4a1818] text-xs font-mono text-red-300">
            <AlertTriangle className="h-4 w-4 text-red-400 shrink-0 mt-0.5" />
            <div>
              <p className="font-bold">Notifications Blocked</p>
              <p className="text-neutral-400 mt-0.5">
                Notification permissions have been blocked in your browser settings. To receive
                push alerts, reset the notification permission for this site.
              </p>
            </div>
          </div>
        ) : push.isSubscribed ? (
          <div className="flex items-center gap-2 text-xs font-mono text-[var(--status-success)]">
            <CheckCircle className="h-4 w-4 shrink-0" />
            <span>Web Push is active and receiving alerts on this device.</span>
          </div>
        ) : (
          <div className="text-xs font-mono text-neutral-400">
            Click &ldquo;Enable push on this device&rdquo; to allow notifications in your browser.
          </div>
        )}
      </MachinedCard>

      {/* 2. Notification Preferences Section */}
      <MachinedCard className="p-3 sm:p-6 space-y-4">
        <div>
          <h3 className="text-sm font-bold uppercase tracking-wider text-white">
            Delivery Preferences
          </h3>
          <p className="text-xs font-mono text-[var(--text-secondary)] mt-0.5">
            Configure which events generate in-app inbox items and browser push alerts.
          </p>
        </div>

        {prefsHook.loading && prefsHook.prefs.length === 0 ? (
          <div className="flex justify-center py-6">
            <CassetteLoader size="sm" />
          </div>
        ) : (
          <div className="divide-y divide-[#1f1f1f] border-t border-b border-[#1f1f1f]">
            <div className="grid grid-cols-[1fr_80px_80px] sm:grid-cols-[1fr_100px_100px] gap-2 py-2 text-[11px] font-mono uppercase text-neutral-400 font-bold">
              <span>Event</span>
              <span className="text-center">Inbox</span>
              <span className="text-center">Push</span>
            </div>

            {USER_NOTIFICATION_EVENTS.map((event) => {
              const pref = prefsHook.prefs.find((p) => p.event === event.id) || {
                event: event.id,
                in_app: true,
                push: true,
              };
              const inAppSaving = prefsHook.savingKey === `${event.id}:in_app`;
              const pushSaving = prefsHook.savingKey === `${event.id}:push`;

              return (
                <div
                  key={event.id}
                  className="grid grid-cols-[1fr_80px_80px] sm:grid-cols-[1fr_100px_100px] gap-2 py-2.5 items-center"
                >
                  <span className="text-xs font-mono text-neutral-200 truncate">
                    {event.label}
                  </span>

                  <div className="flex justify-center">
                    <TactileSwitch
                      id={`pref-${event.id}-in-app`}
                      name={`pref_${event.id}_in_app`}
                      checked={pref.in_app}
                      disabled={inAppSaving}
                      onChange={() => void prefsHook.togglePref(event.id, 'in_app')}
                      ariaLabel={`Toggle inbox notification for ${event.label}`}
                    />
                  </div>

                  <div className="flex justify-center">
                    <TactileSwitch
                      id={`pref-${event.id}-push`}
                      name={`pref_${event.id}_push`}
                      checked={pref.push}
                      disabled={pushSaving}
                      onChange={() => void prefsHook.togglePref(event.id, 'push')}
                      ariaLabel={`Toggle push notification for ${event.label}`}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </MachinedCard>

      {/* 3. Personal Notification Channels Section */}
      <MachinedCard className="p-3 sm:p-6 space-y-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <h3 className="text-sm font-bold uppercase tracking-wider text-white">
              Personal Channels
            </h3>
            <p className="text-xs font-mono text-[var(--text-secondary)] mt-0.5">
              Send updates directly to your own Discord, Telegram, Pushover, or webhook (max 5).
            </p>
          </div>

          <TapeDeckButton
            size="sm"
            variant="amber"
            disabled={channels.channels.length >= 5}
            onClick={editor.openNew}
            icon={<Plus className="h-3.5 w-3.5" />}
          >
            Add Channel ({channels.channels.length}/5)
          </TapeDeckButton>
        </div>

        {channels.loading && (
          <div className="flex justify-center py-6">
            <CassetteLoader size="sm" />
          </div>
        )}

        {!channels.loading && channels.channels.length === 0 && (
          <div className="text-center py-6 text-neutral-500 font-mono text-xs">
            No personal notification channels configured.
          </div>
        )}

        <div className="grid grid-cols-1 gap-3 content-start">
          {channels.channels.map((c) => {
            const events = c.events ?? [];
            const isTesting = channels.testingIds.has(c.id);

            return (
              <div
                key={c.id}
                className="p-3 bg-[#181818] border border-[#222222] rounded-[3px] space-y-2.5"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h4 className="font-bold text-xs sm:text-sm text-white truncate" title={c.name}>
                      {c.name}
                    </h4>
                    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] font-mono text-neutral-400">
                      <span className="px-1.5 py-0.5 rounded-[2px] bg-[#121212] border border-[#2a2a2a] uppercase text-neutral-300">
                        {NOTIFICATION_CHANNEL_TYPE_LABELS[c.channel_type as keyof typeof NOTIFICATION_CHANNEL_TYPE_LABELS] || c.channel_type}
                      </span>
                      <span>
                        {events.length} {events.length === 1 ? 'event' : 'events'}
                      </span>
                    </div>
                  </div>

                  <TactileSwitch
                    id={`user-ch-toggle-${c.id}`}
                    name={`user_ch_toggle_${c.id}`}
                    checked={c.enabled}
                    onChange={(v) => void channels.setEnabled(c, v)}
                    title={c.enabled ? 'Disable channel' : 'Enable channel'}
                    ariaLabel={`Enable notification channel ${c.name}`}
                  />
                </div>

                <div className="text-[11px] font-mono text-neutral-500 break-words">
                  {events.length > 0
                    ? events.map((e) => NOTIFICATION_EVENT_LABELS[e] ?? e).join(', ')
                    : 'No events selected'}
                </div>

                <ActionBar bay className="pt-1">
                  <TapeDeckButton
                    size="sm"
                    disabled={isTesting}
                    onClick={() => void channels.test(c)}
                    icon={
                      isTesting ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <FlaskConical className="h-3 w-3" />
                      )
                    }
                  >
                    Test
                  </TapeDeckButton>
                  <TapeDeckButton
                    size="sm"
                    onClick={() => editor.openEdit(c)}
                    icon={<Pencil className="h-3 w-3" />}
                  >
                    Edit
                  </TapeDeckButton>
                  <ConfirmDangerButton
                    idleLabel="Delete"
                    ariaLabel={`Delete ${c.name}`}
                    icon={<Trash2 className="h-3 w-3" />}
                    onConfirm={() => void channels.remove(c.id)}
                  />
                </ActionBar>
              </div>
            );
          })}
        </div>
      </MachinedCard>

      <UserChannelModal editor={editor} />
    </div>
  );
};
