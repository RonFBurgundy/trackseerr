import React from 'react';
import { TapeTransportBay, TapeDeckButton } from '@/components/ui';
import type { SettingsNavGroup, SettingsTab } from './settingsTabs';

export interface SettingsNavProps {
  groups: SettingsNavGroup[];
  activeTab: SettingsTab;
  onSelect: (tab: SettingsTab) => void;
  /** Tabs whose group is inactive in the current library-manager mode (rendered dimmed). */
  inactiveTabs?: ReadonlySet<SettingsTab>;
}

export const SettingsNav: React.FC<SettingsNavProps> = ({ groups, activeTab, onSelect, inactiveTabs }) => (
  <TapeTransportBay className="flex flex-col sm:flex-row sm:flex-wrap sm:items-stretch gap-3 sm:gap-4 p-2" aria-label="Settings sections">
    {groups.map((group, gi) => {
      const showHeader = !(group.items.length === 1 && group.items[0].label === group.label);
      return (
        <div
          key={group.id}
          className={`flex flex-col gap-1.5 ${gi > 0 ? 'sm:border-l sm:border-[#1f1f1f] sm:pl-4' : ''}`}
        >
          {showHeader && (
            <span className="text-[10px] font-mono uppercase tracking-widest text-neutral-500 px-0.5">
              {group.label}
            </span>
          )}
          <div className="flex flex-wrap items-center gap-1.5">
            {group.items.map((st) => (
              <TapeDeckButton
                key={st.id}
                size="sm"
                active={activeTab === st.id}
                onClick={() => onSelect(st.id)}
                icon={st.icon}
                className={`${inactiveTabs?.has(st.id) ? 'opacity-50' : ''} ${group.items.length > 1 ? 'flex-1 sm:flex-none' : ''}`}
                aria-current={activeTab === st.id ? 'page' : undefined}
              >
                {st.label}
              </TapeDeckButton>
            ))}
          </div>
        </div>
      );
    })}
  </TapeTransportBay>
);
