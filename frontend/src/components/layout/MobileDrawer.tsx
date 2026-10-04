import React, { useEffect } from 'react';
import {
  Compass,
  Inbox,
  Library,
  ListMusic,
  Activity,
  ListTodo,
  Settings,
  X,
  LogOut,
  Shield,
  User as UserIcon,
} from 'lucide-react';
import type { MainTab } from './Navigation';
import type { DeploymentTier, User, UserQuota } from '@/types/models';
import { TapeDeckButton, QuotaBadge } from '@/components/ui';

export interface MobileDrawerProps {
  isOpen: boolean;
  onClose: () => void;
  activeTab: MainTab;
  onTabChange: (tab: MainTab) => void;
  user: User | null;
  quota: UserQuota | null;
  isAdmin?: boolean;
  onLogout?: () => void;
  tier?: DeploymentTier;
}

export const MobileDrawer: React.FC<MobileDrawerProps> = ({
  isOpen,
  onClose,
  activeTab,
  onTabChange,
  user,
  quota,
  isAdmin = false,
  onLogout,
  tier = 'all-in-one',
}) => {
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) {
        onClose();
      }
    };
    if (isOpen) {
      document.body.style.overflow = 'hidden';
      window.addEventListener('keydown', handleKeyDown);
    } else {
      document.body.style.overflow = '';
    }
    return () => {
      document.body.style.overflow = '';
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const navItems: Array<{
    id: MainTab;
    label: string;
    description: string;
    icon: React.ReactNode;
    adminOnly?: boolean;
  }> = [
    {
      id: 'discover',
      label: 'Discover',
      description: 'Explore trending & search catalog',
      icon: <Compass className="h-5 w-5" />,
    },
    {
      id: 'requests',
      label: 'Requests',
      description: 'Manage & monitor your queue',
      icon: <Inbox className="h-5 w-5" />,
    },
    {
      id: 'library',
      label: 'Library',
      description: 'Synced Plex audio collection',
      icon: <Library className="h-5 w-5" />,
      adminOnly: true,
    },
    {
      id: 'playlists',
      label: 'Playlists',
      description: 'Auto-syncing Spotify & Tidal lists',
      icon: <ListMusic className="h-5 w-5" />,
    },
    {
      id: 'activity',
      label: 'Activity',
      description: 'Lidarr & download deck status',
      icon: <Activity className="h-5 w-5" />,
      adminOnly: true,
    },
    {
      id: 'wanted',
      label: 'Wanted',
      description: 'Missing & cutoff-unmet music',
      icon: <ListTodo className="h-5 w-5" />,
      adminOnly: true,
    },
    {
      id: 'settings',
      label: 'Settings',
      description: 'System & server configuration',
      icon: <Settings className="h-5 w-5 text-[#e5a00d]" />,
    },
  ];

  return (
    <div
      className="fixed inset-0 z-50 md:hidden flex justify-end"
      role="dialog"
      aria-modal="true"
      aria-label="Navigation Menu"
    >
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/80 backdrop-blur-sm transition-opacity"
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Drawer Panel */}
      <div className="relative w-full max-w-none bg-[#0c0c0c] border-l border-[#222222] shadow-2xl flex flex-col h-full pt-safe pb-safe z-10">
        {/* Top Header inside Drawer */}
        <div className="flex items-center justify-between px-4 py-3.5 border-b border-[#1f1f1f] bg-[#121212]">
          <div className="flex items-center gap-2.5">
            <img
              src="/trackseerr-logo.svg"
              alt="TrackSeerr"
              className="h-7 w-7 object-contain"
              onError={(e) => {
                (e.currentTarget as HTMLImageElement).src = '/static/trackseerr-logo.svg';
              }}
            />
            <div>
              <span className="text-base font-black tracking-wider uppercase text-white font-mono leading-none">
                Track<span className="text-[#e5a00d]">Seerr</span>
                {tier === 'gateway' && ' Requests'}
                {tier === 'core' && (
                  <span className="ml-2 px-1.5 py-0.5 text-[9px] font-mono font-bold uppercase border border-[var(--border-default)] rounded-[3px] text-[var(--accent-amber)] align-middle">
                    Core
                  </span>
                )}
              </span>
              <p className="text-[10px] text-neutral-400 font-mono tracking-tight uppercase">
                Deck Controls
              </p>
            </div>
          </div>
          <TapeDeckButton
            size="sm"
            onClick={onClose}
            aria-label="Close menu"
            icon={<X className="h-4 w-4" />}
          />
        </div>

        {/* User Status Card & Quota */}
        {user && (
          <div className="p-4 bg-[#121212]/70 border-b border-[#1c1c1c] space-y-3">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 min-w-0">
                <div className="w-8 h-8 rounded-[3px] bg-[#1c1c1c] border border-[#2c2c2c] flex items-center justify-center flex-shrink-0">
                  <UserIcon className="h-4 w-4 text-[#e5a00d]" />
                </div>
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className="text-xs font-mono font-semibold text-white truncate">
                      {user.plex_username}
                    </span>
                    {user.is_admin && (
                      <Shield className="h-3 w-3 text-[#e5a00d] flex-shrink-0" />
                    )}
                  </div>
                  <span className="text-[10px] font-mono text-neutral-400">
                    {user.is_admin ? 'Deck Administrator' : 'Plex Member'}
                  </span>
                </div>
              </div>
            </div>

            <QuotaBadge quota={quota} className="w-full justify-between" />
          </div>
        )}

        {/* Navigation Keys */}
        <div className="flex-1 p-3 flex flex-col gap-2 overflow-y-auto modal-body-scroll">
          <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-mono px-2 pt-1">
            Navigation Deck
          </div>
          {navItems
            .filter((item) => !item.adminOnly || isAdmin)
            .map((item) => {
              const isActive = activeTab === item.id;
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => {
                    onTabChange(item.id);
                    onClose();
                  }}
                  className={`w-full min-h-[56px] flex items-center gap-3 p-3 rounded-[3px] text-left transition-all duration-75 border ${
                    isActive
                      ? 'bg-[#151515] border-[#e5a00d]/40 shadow-[inset_0_1px_3px_rgba(0,0,0,0.8)]'
                      : 'bg-[#121212] border-[#1e1e1e] hover:border-[#333333] hover:bg-[#181818]'
                  }`}
                >
                  <div
                    className={`w-8 h-8 rounded-[3px] flex items-center justify-center flex-shrink-0 border ${
                      isActive
                        ? 'bg-[#0f0f0f] border-[#e5a00d] text-[#e5a00d]'
                        : 'bg-[#181818] border-[#262626] text-neutral-300'
                    }`}
                  >
                    {item.icon}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between">
                      <span
                        className={`text-xs font-bold uppercase tracking-wider font-mono ${
                          isActive ? 'text-[#e5a00d]' : 'text-neutral-200'
                        }`}
                      >
                        {item.label}
                      </span>
                      {isActive && (
                        <span className="h-1.5 w-1.5 rounded-full bg-[#e5a00d] shadow-[0_0_6px_rgba(229,160,13,0.8)]" />
                      )}
                    </div>
                    <p className="text-[11px] text-neutral-400 font-mono truncate">
                      {item.description}
                    </p>
                  </div>
                </button>
              );
            })}
        </div>

        {/* Footer with Logout */}
        <div className="p-3 border-t border-[#1f1f1f] bg-[#0e0e0e] space-y-2">
          {user && onLogout && (
            <TapeDeckButton
              size="md"
              variant="default"
              onClick={() => {
                onClose();
                onLogout();
              }}
              icon={<LogOut className="h-4 w-4 text-red-400" />}
              className="w-full justify-center text-xs"
            >
              Sign Out
            </TapeDeckButton>
          )}
          <div className="text-center pt-1">
            <span className="text-[9px] uppercase tracking-widest text-neutral-600 font-mono">
              TrackSeerr Analog Deck v1.0
            </span>
          </div>
        </div>
      </div>
    </div>
  );
};
