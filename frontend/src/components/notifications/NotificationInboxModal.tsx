import React, { useEffect, useState } from 'react';
import { Bell, CheckCheck, Trash2, ExternalLink, RefreshCw } from 'lucide-react';
import { ObsidianModal, TapeDeckButton, CassetteLoader } from '@/components/ui';
import type { UserNotificationItem } from '@/types/userNotifications';
import type { UseUserNotificationsReturn } from '@/hooks/useUserNotifications';

export interface NotificationInboxModalProps {
  isOpen: boolean;
  onClose: () => void;
  inboxHook: UseUserNotificationsReturn;
}

function formatNotificationTime(isoString: string): string {
  try {
    const date = new Date(isoString);
    const now = new Date();
    const diffSec = Math.floor((now.getTime() - date.getTime()) / 1000);

    if (diffSec < 60) return 'Just now';
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
    if (diffSec < 604800) return `${Math.floor(diffSec / 86400)}d ago`;

    return date.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
    });
  } catch {
    return isoString;
  }
}

export const NotificationInboxModal: React.FC<NotificationInboxModalProps> = ({
  isOpen,
  onClose,
  inboxHook,
}) => {
  const {
    inbox,
    unreadCount,
    loading,
    fetchInbox,
    markRead,
    markAllRead,
    removeNotification,
  } = inboxHook;

  const [page, setPage] = useState<number>(1);

  useEffect(() => {
    if (isOpen) {
      setPage(1);
      void fetchInbox(1, 30);
    }
  }, [isOpen, fetchInbox]);

  const handleItemClick = async (item: UserNotificationItem): Promise<void> => {
    if (!item.read_at) {
      void markRead(item.id);
    }
    if (item.link) {
      onClose();
      if (item.link.startsWith('#')) {
        window.location.hash = item.link;
      } else if (item.link.startsWith('/')) {
        window.location.href = item.link;
      }
    }
  };

  const handleLoadMore = (): void => {
    const nextPage = page + 1;
    setPage(nextPage);
    void fetchInbox(nextPage, 30);
  };

  const items = inbox?.items || [];
  const total = inbox?.total || 0;
  const hasMore = items.length < total;

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="Notification Inbox"
      subtitle={
        unreadCount > 0
          ? `${unreadCount} unread notification${unreadCount === 1 ? '' : 's'}`
          : 'All caught up'
      }
      footer={
        <>
          {unreadCount > 0 && (
            <TapeDeckButton
              size="sm"
              variant="default"
              icon={<CheckCheck className="h-4 w-4 text-[var(--accent-amber)]" />}
              onClick={() => void markAllRead()}
            >
              Mark all read
            </TapeDeckButton>
          )}
          <TapeDeckButton size="sm" onClick={onClose}>
            Close
          </TapeDeckButton>
        </>
      }
    >
      <div className="space-y-3 min-h-[200px]">
        {loading && items.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-neutral-400">
            <CassetteLoader size="md" />
            <span className="text-xs font-mono uppercase mt-3">Loading inbox...</span>
          </div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <Bell className="h-10 w-10 text-neutral-600 mb-2 stroke-[1.5]" />
            <span className="text-sm font-mono font-bold uppercase text-neutral-300">
              No notifications
            </span>
            <span className="text-xs font-mono text-neutral-500 mt-1">
              You will receive updates here for your requests and issues.
            </span>
          </div>
        ) : (
          <div className="space-y-2">
            {items.map((item) => {
              const isUnread = !item.read_at;
              return (
                <div
                  key={item.id}
                  className={`group relative flex items-start gap-3 p-3 rounded-[3px] border transition-colors ${
                    isUnread
                      ? 'bg-[#181818] border-[var(--border-default)] hover:border-[var(--accent-amber)]/60'
                      : 'bg-[#121212] border-[#222222] hover:border-[#333333]'
                  }`}
                >
                  {/* Status Indicator */}
                  <div className="pt-1 flex-shrink-0">
                    <span
                      className={`block w-2 h-2 rounded-full ${
                        isUnread
                          ? 'bg-[var(--accent-amber)] shadow-[0_0_6px_rgba(229,160,13,0.8)]'
                          : 'bg-neutral-600'
                      }`}
                      aria-label={isUnread ? 'Unread notification' : 'Read notification'}
                    />
                  </div>

                  {/* Body / Content */}
                  <div
                    className="flex-1 min-w-0 cursor-pointer"
                    onClick={() => void handleItemClick(item)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        void handleItemClick(item);
                      }
                    }}
                  >
                    <div className="flex items-baseline justify-between gap-2">
                      <h4
                        className={`text-xs font-mono uppercase font-bold tracking-wider truncate ${
                          isUnread ? 'text-white' : 'text-neutral-300'
                        }`}
                      >
                        {item.title}
                      </h4>
                      <span className="text-[10px] font-mono text-neutral-500 shrink-0">
                        {formatNotificationTime(item.created_at)}
                      </span>
                    </div>

                    <p className="text-xs text-neutral-400 mt-1 whitespace-pre-wrap break-words">
                      {item.message}
                    </p>

                    {item.link && (
                      <div className="inline-flex items-center gap-1 text-[11px] font-mono text-[var(--accent-amber)] mt-1.5 hover:underline">
                        <span>View details</span>
                        <ExternalLink className="h-3 w-3" />
                      </div>
                    )}
                  </div>

                  {/* Actions */}
                  <div className="flex items-center gap-1 shrink-0 pt-0.5">
                    {isUnread && (
                      <TapeDeckButton
                        size="sm"
                        className="w-7 h-7 !min-h-0 p-0 text-neutral-400 hover:text-white"
                        title="Mark read"
                        aria-label="Mark notification as read"
                        onClick={(e) => {
                          e.stopPropagation();
                          void markRead(item.id);
                        }}
                        icon={<CheckCheck className="h-3.5 w-3.5" />}
                      />
                    )}
                    <TapeDeckButton
                      size="sm"
                      className="w-7 h-7 !min-h-0 p-0 text-neutral-500 hover:text-red-400"
                      title="Delete notification"
                      aria-label="Delete notification"
                      onClick={(e) => {
                        e.stopPropagation();
                        void removeNotification(item.id);
                      }}
                      icon={<Trash2 className="h-3.5 w-3.5" />}
                    />
                  </div>
                </div>
              );
            })}

            {hasMore && (
              <div className="pt-2 text-center">
                <TapeDeckButton
                  size="sm"
                  variant="default"
                  disabled={loading}
                  onClick={handleLoadMore}
                  icon={loading ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : undefined}
                >
                  {loading ? 'Loading...' : 'Load more'}
                </TapeDeckButton>
              </div>
            )}
          </div>
        )}
      </div>
    </ObsidianModal>
  );
};
