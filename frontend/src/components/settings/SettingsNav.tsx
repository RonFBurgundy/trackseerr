import React from 'react';
import { TabStrip, TapeDeckButton } from '@/components/ui';
import type { SettingsNavGroup, SettingsTab } from './settingsTabs';

export interface SettingsNavProps {
  groups: SettingsNavGroup[];
  activeTab: SettingsTab;
  onSelect: (tab: SettingsTab) => void;
  /** Tabs whose group is inactive in the current library-manager mode (rendered dimmed). */
  inactiveTabs?: ReadonlySet<SettingsTab>;
}

export const SettingsNav: React.FC<SettingsNavProps> = ({ groups, activeTab, onSelect, inactiveTabs }) => (
  <TabStrip
    className="sm:!flex-wrap sm:!overflow-visible sm:!items-stretch sm:!gap-x-6 sm:!gap-y-3 sm:!w-full !gap-3 p-1.5 sm:p-2"
    aria-label="Settings sections"
  >
    {groups.map((group, gi) => {
      const showHeader = !(group.items.length === 1 && group.items[0].label === group.label);
      return (
        <div
          key={group.id}
          className={`flex shrink-0 flex-col gap-1.5 ${
            gi > 0 ? 'border-l border-[#1f1f1f] pl-3 sm:border-l-0 sm:pl-0' : ''
          }`}
        >
          {showHeader && (
            <span className="hidden sm:block text-[10px] font-mono uppercase tracking-widest text-neutral-500 px-0.5 whitespace-nowrap">
              {group.label}
            </span>
          )}
          <div className="flex flex-nowrap sm:flex-wrap items-stretch gap-1.5">
            {group.items.map((st) => (
              <TapeDeckButton
                key={st.id}
                size="sm"
                active={activeTab === st.id}
                onClick={() => onSelect(st.id)}
                icon={st.icon}
                className={inactiveTabs?.has(st.id) ? 'opacity-50' : ''}
                aria-current={activeTab === st.id ? 'page' : undefined}
              >
                {st.label}
              </TapeDeckButton>
            ))}
          </div>
        </div>
      );
    })}
  </TabStrip>
);
