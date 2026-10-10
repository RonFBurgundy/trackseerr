import React from 'react';
import {
  Compass,
  Inbox,
  Library,
  ListMusic,
  Activity,
  ListTodo,
  Settings,
  LogIn,
  LogOut,
  Shield,
  Menu,
  X,
  Bell,
} from 'lucide-react';
import type { DeploymentTier, User, UserQuota } from '@/types/models';
import type { MainTab } from '@/hooks/useAppRoute';
import { TapeDeckButton, TapeTransportBay, QuotaBadge } from '@/components/ui';
import { useSystemActivity } from '@/hooks/useSystemActivity';
import { BrandActivity } from './BrandActivity';

export interface HeaderProps {
  user: User | null;
  quota: UserQuota | null;
  /** Hub navigator state; the Menu key is shown on every viewport. */
  isMenuOpen?: boolean;
  onToggleMenu?: () => void;
  onLogin: () => void;
  onLogout: () => void;
  activeTab?: MainTab;
  onTabChange?: (tab: MainTab) => void;
  isAdmin?: boolean;
  /** Library-health findings awaiting review, shown on the Activity key (admin only). */
  reviewCount?: number;
  /** Open issues awaiting an admin, added to the Requests key's badge (admin only). */
  issuesOpenCount?: number;
  /** The user's own issues with unseen admin activity, shown on the Requests key. */
  issuesUnreadCount?: number;
  /** User notifications unread count for the bell badge. */
  unreadNotificationsCount?: number;
  /** Handler to open user notification inbox. */
  onOpenInbox?: () => void;
  tier?: DeploymentTier;
  /** Admin with a usable session: the logo shows task activity and opens the activity popover (polls /api/system/activity). */
  activityEnabled?: boolean;
  /** Popover link target: Settings > System > Tasks. */
  onOpenTasks?: () => void;
  /** Admin only: small badge on the logo when a newer release is available. */
  updateAvailable?: boolean;
}

export const Header: React.FC<HeaderProps> = ({
  user,
  quota,
  isMenuOpen = false,
  onToggleMenu,
  onLogin,
  onLogout,
  activeTab,
  onTabChange,
  isAdmin = false,
  reviewCount = 0,
  issuesOpenCount = 0,
  issuesUnreadCount = 0,
  unreadNotificationsCount = 0,
  onOpenInbox,
  tier = 'all-in-one',
  activityEnabled = false,
  onOpenTasks,
  updateAvailable = false,
}) => {
  // One poll shared by the desktop and mobile logos.
  const activity = useSystemActivity(activityEnabled && user !== null);
  const showActivity = activityEnabled && user !== null && onOpenTasks !== undefined;
  const isGateway = tier === 'gateway';
  const brandSuffix = isGateway ? <span className="text-white"> Requests</span> : null;
  const coreBadge =
    tier === 'core' ? (
      <span className="ml-2 px-1.5 py-0.5 text-[9px] font-mono font-bold uppercase border border-[var(--border-default)] rounded-[3px] text-[var(--accent-amber)] align-middle">
        Core
      </span>
    ) : null;
  /** Count chip per main key: Activity = review findings, Requests = open issues (admin) or unseen issue replies (user). */
  const badgeFor = (id: MainTab): number => {
    if (id === 'activity') return reviewCount;
    if (id === 'requests') return isAdmin ? issuesOpenCount : issuesUnreadCount;
    return 0;
  };
  const navItems: Array<{ id: MainTab; label: string; icon: React.ReactNode; adminOnly?: boolean }> = [
    { id: 'discover', label: 'Discover', icon: <Compass className="h-4 w-4" /> },
    { id: 'requests', label: 'Requests', icon: <Inbox className="h-4 w-4" /> },
    { id: 'library', label: 'Library', icon: <Library className="h-4 w-4" />, adminOnly: true },
    { id: 'playlists', label: 'Playlists', icon: <ListMusic className="h-4 w-4" /> },
    { id: 'activity', label: 'Activity', icon: <Activity className="h-4 w-4" />, adminOnly: true },
    { id: 'wanted', label: 'Wanted', icon: <ListTodo className="h-4 w-4" />, adminOnly: true },
    { id: 'settings', label: 'Settings', icon: <Settings className="h-4 w-4" /> },
  ];
  const mobileNavItems = navItems.filter((item) => item.id !== 'settings');

  return (
    <header className="sticky top-0 z-40 w-full flex-shrink-0 bg-[#0a0a0a]/95 backdrop-blur-md border-b border-[#1f1f1f] pt-safe">
      <div className="max-w-7xl mx-auto px-3 sm:px-6 h-[52px] md:h-16 flex items-center justify-between gap-2 sm:gap-4">
        {/* Brand / Logo */}
        {/* Desktop Brand */}
        <div className="hidden md:flex items-center gap-3 select-none flex-shrink-0">
          <div className="relative inline-flex items-center">
            {showActivity ? (
              <BrandActivity
                activity={activity}
                onOpenTasks={onOpenTasks}
                className="flex items-center justify-center h-8 w-8 rounded-[3px] focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--accent-amber)]"
              >
                <img
                  src="/trackseerr-logo.svg"
                  alt=""
                  className="h-8 w-8 object-contain"
                  onError={(e) => {
                    e.currentTarget.src = '/static/trackseerr-logo.svg';
                  }}
                />
              </BrandActivity>
            ) : (
              <img
                src="/trackseerr-logo.svg"
                alt="TrackSeerr"
                className="h-8 w-8 object-contain"
                onError={(e) => {
                  e.currentTarget.src = '/static/trackseerr-logo.svg';
                }}
              />
            )}
            {isAdmin && updateAvailable && (
              <span
                className="absolute -top-1 -right-1 w-2.5 h-2.5 rounded-full bg-[var(--accent-amber)] ring-2 ring-[#0a0a0a]"
                title="TrackSeerr update available"
                aria-label="Software update available"
              />
            )}
          </div>
          <span className="hidden lg:inline text-base sm:text-lg font-black tracking-wider uppercase text-white font-mono leading-none">
            Track<span className="text-[#e5a00d]">Seerr</span>
            {brandSuffix}
            {coreBadge}
          </span>
        </div>

        {/* Mobile brand: compact logo key (always visible on phones). Admins get the task-activity popover; others return to Discover. */}
        {showActivity ? (
          <BrandActivity
            activity={activity}
            onOpenTasks={onOpenTasks}
            className="md:hidden relative flex items-center justify-center w-9 h-9 rounded-[3px] border border-[#222222] bg-[#121212] hover:border-[#383838] active:translate-y-[1px] transition-all flex-shrink-0 after:absolute after:content-[''] after:-inset-[2px]"
          >
            <img
              src="/trackseerr-logo.svg"
              alt=""
              width={24}
              height={24}
              className="h-6 w-6 object-contain"
              onError={(e) => {
                e.currentTarget.src = '/static/trackseerr-logo.svg';
              }}
            />
            {isAdmin && updateAvailable && (
              <span
                className="absolute -top-1 -right-1 w-2.5 h-2.5 rounded-full bg-[var(--accent-amber)] ring-2 ring-[#0a0a0a]"
                title="TrackSeerr update available"
                aria-label="Software update available"
              />
            )}
          </BrandActivity>
        ) : (
          <button
            type="button"
            onClick={() => onTabChange?.('discover')}
            className="md:hidden relative flex items-center justify-center w-9 h-9 rounded-[3px] border border-[#222222] bg-[#121212] hover:border-[#383838] active:translate-y-[1px] transition-all flex-shrink-0 after:absolute after:content-[''] after:-inset-[2px]"
            aria-label="TrackSeerr Home"
            title="TrackSeerr Home"
          >
          <img
            src="/trackseerr-logo.svg"
            alt=""
            width={24}
            height={24}
            className="h-6 w-6 object-contain"
            onError={(e) => {
              e.currentTarget.src = '/static/trackseerr-logo.svg';
            }}
          />
          {isAdmin && updateAvailable && (
            <span
              className="absolute -top-1 -right-1 w-2.5 h-2.5 rounded-full bg-[var(--accent-amber)] ring-2 ring-[#0a0a0a]"
              title="TrackSeerr update available"
              aria-label="Software update available"
            />
          )}
          </button>
        )}

        {/* Desktop Sticky Navigation Buttons */}
        {user && activeTab && onTabChange && (
          <div className="hidden md:flex items-center gap-1">
            <TapeTransportBay className="p-1">
              <div className="flex items-center gap-1">
                {navItems
                  .filter((item) => !item.adminOnly || isAdmin)
                  .map((item) => {
                    const isActive = activeTab === item.id;
                    return (
                      <TapeDeckButton
                        key={item.id}
                        size="sm"
                        active={isActive}
                        onClick={() => onTabChange(item.id)}
                        icon={item.icon}
                        className="rounded-[3px]"
                        collapseLabel="xl"
                        title={item.label}
                      >
                        {item.label}
                        {badgeFor(item.id) > 0 && (
                          <span className="ml-1 px-1 rounded-[3px] bg-[var(--accent-amber)] text-[10px] font-mono font-bold text-black">
                            {badgeFor(item.id)}
                          </span>
                        )}
                      </TapeDeckButton>
                    );
                  })}
              </div>
            </TapeTransportBay>
          </div>
        )}

        {/* Mobile shortcut bay: icon-only keys inside the header; the hub stays the full navigator */}
        {user && activeTab && onTabChange && (
          <div className="md:hidden flex flex-1 min-w-0 justify-center">
            <TapeTransportBay className="p-[2px] mx-auto">
              <div className="flex items-stretch gap-1">
                {mobileNavItems
                  .filter((item) => !item.adminOnly || isAdmin)
                  .map((item) => {
                    const isActive = activeTab === item.id;
                    return (
                      <TapeDeckButton
                        key={item.id}
                        size="sm"
                        active={isActive}
                        onClick={() => onTabChange(item.id)}
                        icon={item.icon}
                        className="w-9 h-9 !min-h-0 p-0 rounded-[3px] shrink-0"
                        aria-label={item.label}
                        aria-current={isActive ? 'page' : undefined}
                        title={item.label}
                      >
                        {badgeFor(item.id) > 0 && (
                          <span
                            className="absolute -top-1 -right-1 min-w-[14px] px-0.5 rounded-[3px] bg-[var(--accent-amber)] text-[9px] leading-[14px] font-mono font-bold text-black text-center"
                            aria-hidden="true"
                          >
                            {badgeFor(item.id)}
                          </span>
                        )}
                      </TapeDeckButton>
                    );
                  })}
              </div>
            </TapeTransportBay>
          </div>
        )}

        {/* User Status / Actions (Desktop) */}
        <div className="hidden md:flex items-center gap-3 flex-shrink-0">
          {user && <QuotaBadge quota={quota} className="hidden sm:inline-flex" />}

          {user ? (
            <div className="flex items-center gap-2">
              <div className="hidden lg:flex items-center gap-2 px-3 py-1.5 bg-[#141414] border border-[#222222] rounded-[3px]">
                {user.is_admin && <Shield className="h-3.5 w-3.5 text-[#e5a00d]" />}
                <span className="text-xs font-mono font-medium text-neutral-200">
                  {user.username}
                </span>
              </div>
              {onOpenInbox && (
                <TapeDeckButton
                  size="sm"
                  variant="default"
                  onClick={onOpenInbox}
                  title="Notifications"
                  aria-label={`Notifications${unreadNotificationsCount > 0 ? ` (${unreadNotificationsCount} unread)` : ''}`}
                  icon={<Bell className="h-4 w-4 text-neutral-300" />}
                  className="relative"
                >
                  {unreadNotificationsCount > 0 && (
                    <span className="ml-1 px-1 rounded-[3px] bg-[var(--accent-amber)] text-[10px] font-mono font-bold text-black">
                      {unreadNotificationsCount}
                    </span>
                  )}
                </TapeDeckButton>
              )}
              <TapeDeckButton
                size="sm"
                variant="default"
                onClick={onLogout}
                title="Sign Out"
                aria-label="Sign out"
                collapseLabel="lg"
                icon={<LogOut className="h-4 w-4 text-neutral-400" />}
              >
                Logout
              </TapeDeckButton>
            </div>
          ) : (
            <TapeDeckButton
              size="sm"
              variant="amber"
              onClick={onLogin}
              icon={<LogIn className="h-4 w-4" />}
            >
              Sign In
            </TapeDeckButton>
          )}
        </div>

        {/* Hub key (every viewport): right edge on phones, left edge on desktop. Sign In stands in when signed out. */}
        {user ? (
          <div className="order-last md:order-first ml-auto md:ml-0 flex items-center gap-1.5 flex-shrink-0">
            <TapeDeckButton
              size="sm"
              variant="default"
              active={isMenuOpen}
              onClick={onToggleMenu}
              title={isMenuOpen ? 'Close Menu' : 'Open Menu'}
              aria-label={`${isMenuOpen ? 'Close Menu' : 'Open Menu'}${unreadNotificationsCount > 0 ? ` (${unreadNotificationsCount} unread notifications)` : ''}`}
              aria-expanded={isMenuOpen}
              aria-haspopup="dialog"
              icon={
                isMenuOpen ? (
                  <X className="h-4 w-4 text-[#e5a00d]" />
                ) : (
                  <Menu className="h-4 w-4 text-neutral-300" />
                )
              }
              className="relative w-9 h-9 !min-h-0 p-0 rounded-[3px] shrink-0 md:w-11 md:h-11"
            >
              {unreadNotificationsCount > 0 && (
                <span
                  className="md:hidden absolute -top-1 -right-1 w-2 h-2 rounded-[2px] bg-[var(--accent-amber)]"
                  aria-hidden="true"
                />
              )}
            </TapeDeckButton>
          </div>
        ) : (
          <div className="md:hidden flex items-center flex-shrink-0">
            <TapeDeckButton
              size="sm"
              variant="amber"
              onClick={onLogin}
              icon={<LogIn className="h-4 w-4" />}
            >
              Sign In
            </TapeDeckButton>
          </div>
        )}
      </div>
    </header>
  );
};
