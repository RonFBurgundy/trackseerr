import React from 'react';
import { Compass, Inbox, Library, ListMusic, Activity, ListTodo, Settings } from 'lucide-react';
import { TapeTransportBay, TapeDeckButton } from '@/components/ui';

import type { MainTab } from '@/hooks/useAppRoute';

export type { MainTab };

export interface NavigationProps {
  activeTab: MainTab;
  onTabChange: (tab: MainTab) => void;
  isAdmin?: boolean;
}

export const Navigation: React.FC<NavigationProps> = ({
  activeTab,
  onTabChange,
  isAdmin = false,
}) => {
  const navItems: Array<{ id: MainTab; label: string; icon: React.ReactNode; adminOnly?: boolean }> = [
    { id: 'discover', label: 'Discover', icon: <Compass className="h-4 w-4" /> },
    { id: 'requests', label: 'Requests', icon: <Inbox className="h-4 w-4" /> },
    { id: 'library', label: 'Library', icon: <Library className="h-4 w-4" />, adminOnly: true },
    { id: 'playlists', label: 'Playlists', icon: <ListMusic className="h-4 w-4" /> },
    { id: 'activity', label: 'Activity', icon: <Activity className="h-4 w-4" />, adminOnly: true },
    { id: 'wanted', label: 'Wanted', icon: <ListTodo className="h-4 w-4" />, adminOnly: true },
    { id: 'settings', label: 'Settings', icon: <Settings className="h-4 w-4" /> },
  ];

  return (
    <nav aria-label="Main sections" className="hidden md:block w-full max-w-7xl mx-auto px-4 sm:px-6 pt-4 pb-2">
      <TapeTransportBay className="flex items-center justify-between gap-1 overflow-x-auto p-1.5">
        <div className="flex items-center gap-1.5 w-full sm:w-auto">
          {navItems
            .filter((item) => !item.adminOnly || isAdmin)
            .map((item) => {
              const isActive = activeTab === item.id;
              return (
                <TapeDeckButton
                  key={item.id}
                  size="md"
                  active={isActive}
                  onClick={() => onTabChange(item.id)}
                  icon={item.icon}
                  className="flex-1 sm:flex-initial"
                >
                  {item.label}
                </TapeDeckButton>
              );
            })}
        </div>
      </TapeTransportBay>
    </nav>
  );
};
