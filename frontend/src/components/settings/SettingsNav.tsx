import React, { useState } from 'react';
import { ChevronLeft } from 'lucide-react';
import { TabStrip, TapeDeckButton } from '@/components/ui';
import { settingsRouteFor } from '@/hooks/useAppRoute';
import type { SettingsRoute, SettingsSection } from '@/hooks/useAppRoute';
import { hasChildRow } from './settingsTabs';
import type { SettingsSectionNode } from './settingsTabs';

export interface SettingsNavProps {
  sections: SettingsSectionNode[];
  /** Resolved current location. */
  route: SettingsRoute;
  onNavigate: (route: SettingsRoute) => void;
  /** Section or leaf ids rendered dimmed (inactive in the current library-manager mode). */
  inactiveIds?: ReadonlySet<string>;
}

const STRIP_CLASS = '!gap-1.5 p-1.5 sm:p-2';

/**
 * One row of keys. Top level: the sections. Inside a section with several pages the row is replaced by
 * a back key plus that section's page keys ("drill-down replaces"), so there is never a second row.
 */
export const SettingsNav: React.FC<SettingsNavProps> = ({ sections, route, onNavigate, inactiveIds }) => {
  // Remembers which section the user backed out of; moving to another section closes the picker by itself.
  const [pickingFrom, setPickingFrom] = useState<SettingsSection | null>(null);
  const active = sections.find((s) => s.id === route.sub);
  const activeLeaf = 'leaf' in route ? route.leaf : undefined;
  const drilled = active !== undefined && hasChildRow(active) && pickingFrom !== active.id;

  if (active && drilled) {
    return (
      <TabStrip className={STRIP_CLASS} aria-label={`${active.label} pages`}>
        <TapeDeckButton
          size="sm"
          onClick={() => setPickingFrom(active.id)}
          icon={<ChevronLeft className="h-3.5 w-3.5" />}
          aria-label="Back to settings sections"
        >
          Settings
        </TapeDeckButton>
        {active.leaves.map((l) => (
          <TapeDeckButton
            key={l.id}
            size="sm"
            active={activeLeaf === l.id}
            onClick={() => onNavigate(settingsRouteFor(active.id, l.id))}
            icon={l.icon}
            className={inactiveIds?.has(l.id) ? 'opacity-50' : ''}
            aria-current={activeLeaf === l.id ? 'page' : undefined}
          >
            {l.label}
          </TapeDeckButton>
        ))}
      </TabStrip>
    );
  }

  return (
    <TabStrip className={STRIP_CLASS} aria-label="Settings sections">
      {sections.map((s) => {
        const visibleLeafIds = s.leaves.map((l) => l.id);
        const dimmed =
          visibleLeafIds.length > 0
            ? visibleLeafIds.every((id) => inactiveIds?.has(id))
            : Boolean(inactiveIds?.has(s.id));
        return (
          <TapeDeckButton
            key={s.id}
            size="sm"
            active={route.sub === s.id}
            onClick={() => {
              setPickingFrom(null);
              // Opens the section's first visible page; the section already open keeps its current page.
              onNavigate(route.sub === s.id ? route : settingsRouteFor(s.id, s.leaves[0]?.id));
            }}
            icon={s.icon}
            className={dimmed ? 'opacity-50' : ''}
            aria-current={route.sub === s.id ? 'page' : undefined}
          >
            {s.label}
          </TapeDeckButton>
        );
      })}
    </TabStrip>
  );
};
