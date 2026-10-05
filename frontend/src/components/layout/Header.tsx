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
} from 'lucide-react';
import type { DeploymentTier, User, UserQuota } from '@/types/models';
import type { MainTab } from '@/hooks/useAppRoute';
import { TapeDeckButton, TapeTransportBay, QuotaBadge } from '@/components/ui';

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
  tier?: DeploymentTier;
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
  tier = 'all-in-one',
}) => {
  const isGateway = tier === 'gateway';
  const brandSuffix = isGateway ? <span className="text-white"> Requests</span> : null;
  const coreBadge =
    tier === 'core' ? (
      <span className="ml-2 px-1.5 py-0.5 text-[9px] font-mono font-bold uppercase border border-[var(--border-default)] rounded-[3px] text-[var(--accent-amber)] align-middle">
        Core
      </span>
    ) : null;
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
          <img
            src="/trackseerr-logo.svg"
            alt="TrackSeerr"
            className="h-8 w-8 object-contain"
            onError={(e) => {
              e.currentTarget.src = '/static/trackseerr-logo.svg';
            }}
          />
          <span className="hidden lg:inline text-base sm:text-lg font-black tracking-wider uppercase text-white font-mono leading-none">
            Track<span className="text-[#e5a00d]">Seerr</span>
            {brandSuffix}
            {coreBadge}
          </span>
        </div>

        {/* Mobile brand: compact logo key (always visible on phones) that returns to Discover */}
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
        </button>

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
                      </TapeDeckButton>
                    );
                  })}
              </div>
            </TapeTransportBay>
          </div>
        )}

        {/* Mobile shortcut bay: icon-only keys inside the header; the hub stays the full navigator */}
        {user && activeTab && onTabChange && (
          <div className="md:hidden flex flex-1 min-w-0 overflow-x-auto tab-strip">
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
                      />
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
                  {user.plex_username}
                </span>
              </div>
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
          <div className="order-last md:order-first ml-auto md:ml-0 flex items-center flex-shrink-0">
            <TapeDeckButton
              size="sm"
              variant="default"
              active={isMenuOpen}
              onClick={onToggleMenu}
              title={isMenuOpen ? 'Close Menu' : 'Open Menu'}
              aria-label={isMenuOpen ? 'Close Menu' : 'Open Menu'}
              aria-expanded={isMenuOpen}
              aria-haspopup="dialog"
              icon={
                isMenuOpen ? (
                  <X className="h-4 w-4 text-[#e5a00d]" />
                ) : (
                  <Menu className="h-4 w-4 text-neutral-300" />
                )
              }
              className="w-9 h-9 !min-h-0 p-0 rounded-[3px] shrink-0 md:w-11 md:h-11"
            />
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
