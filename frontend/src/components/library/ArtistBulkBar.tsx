import React, { useState } from 'react';
import { Check, Eye, EyeOff, Loader2, X } from 'lucide-react';
import type { QualityProfile } from '@/types/models';
import { MONITOR_OPTION_LABELS, type MonitorOption } from '@/types/monitoring';
import type { UseBulkSelectionReturn } from '@/hooks/useBulkSelection';
import type { UseArtistBulkEditReturn } from '@/hooks/useArtistBulkEdit';
import { ActionBar, MachinedCard, MonitorOptionSelect, TapeDeckButton } from '@/components/ui';
import { inputClass } from '@/components/settings/formClasses';

const NO_PROFILE = '__none__';

export interface ArtistBulkBarProps {
  selection: UseBulkSelectionReturn;
  /** Artists in the current list (what "All N artists" counts). */
  total: number;
  /** False while a search or monitored-only filter is on: server-side "all" ignores filters. */
  canSelectAll: boolean;
  edit: UseArtistBulkEditReturn;
  profiles: QualityProfile[];
  profilesLoading: boolean;
}

/** Lidarr-style mass editor for the artists grid. */
export const ArtistBulkBar: React.FC<ArtistBulkBarProps> = ({
  selection,
  total,
  canSelectAll,
  edit,
  profiles,
  profilesLoading,
}) => {
  const [option, setOption] = useState<MonitorOption>('all');
  const [applyToAlbums, setApplyToAlbums] = useState<boolean>(true);
  const [profile, setProfile] = useState<string>('');

  const count = selection.count(total);
  const empty = count === 0;
  const disabled = empty || edit.busy;
  const countLabel = selection.allMatching
    ? `All ${total.toLocaleString()} artists`
    : `${count.toLocaleString()} selected`;

  if (edit.pending) {
    const { pending } = edit;
    return (
      <MachinedCard role="alertdialog" aria-label="Confirm bulk edit" className="p-3 space-y-3 border-[#e5a00d]/40">
        <p className="text-xs font-mono text-white">
          {pending.summary} for{' '}
          {selection.allMatching
            ? `all ${pending.targetCount.toLocaleString()} artists`
            : `${pending.targetCount.toLocaleString()} selected ${pending.targetCount === 1 ? 'artist' : 'artists'}`}
          ?
        </p>
        {pending.patch.apply_monitor_to_albums === true && (
          <p className="text-[11px] font-mono text-neutral-400">
            This also changes monitoring on every existing album of each artist.
          </p>
        )}
        <ActionBar align="end">
          <TapeDeckButton
            variant="amber"
            disabled={edit.busy}
            onClick={edit.confirm}
            icon={edit.busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
          >
            Confirm
          </TapeDeckButton>
          <TapeDeckButton disabled={edit.busy} onClick={edit.cancel}>
            Cancel
          </TapeDeckButton>
        </ActionBar>
      </MachinedCard>
    );
  }

  const apply = (patch: Parameters<UseArtistBulkEditReturn['request']>[0], summary: string): void =>
    edit.request(patch, summary);

  return (
    <MachinedCard className="p-3 space-y-3" aria-label="Bulk edit artists">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div className="min-w-0">
          <span className="text-xs font-mono font-bold uppercase text-[#e5a00d]">{countLabel}</span>
          {!canSelectAll && (
            <p className="text-[11px] font-mono text-neutral-500">Clear search and filters to select every artist.</p>
          )}
        </div>
        <ActionBar align="end">
          {canSelectAll && !selection.allMatching && (
            <TapeDeckButton size="sm" onClick={selection.selectAllMatching}>
              {`All ${total.toLocaleString()} artists`}
            </TapeDeckButton>
          )}
          <TapeDeckButton size="sm" onClick={selection.clear} disabled={empty}>
            Clear
          </TapeDeckButton>
          <TapeDeckButton size="sm" onClick={selection.exit} icon={<X className="h-3.5 w-3.5" />}>
            Done
          </TapeDeckButton>
        </ActionBar>
      </div>

      <ActionBar>
        <TapeDeckButton
          size="sm"
          disabled={disabled}
          onClick={() => apply({ monitored: true }, 'Monitor')}
          icon={<Eye className="h-3.5 w-3.5" />}
        >
          Monitor
        </TapeDeckButton>
        <TapeDeckButton
          size="sm"
          disabled={disabled}
          onClick={() => apply({ monitored: false }, 'Unmonitor')}
          icon={<EyeOff className="h-3.5 w-3.5" />}
        >
          Unmonitor
        </TapeDeckButton>
      </ActionBar>

      <div className="flex flex-col sm:flex-row sm:items-center gap-2">
        <MonitorOptionSelect
          value={option}
          onChange={setOption}
          disabled={edit.busy}
          aria-label="Monitor option"
          className="sm:w-56"
        />
        <label className="flex items-center gap-2 min-h-[44px] sm:min-h-0 text-xs font-mono text-neutral-300 cursor-pointer">
          <input
            type="checkbox"
            checked={applyToAlbums}
            onChange={(e) => setApplyToAlbums(e.target.checked)}
            className="h-5 w-5 accent-[#e5a00d]"
          />
          Also apply to existing albums
        </label>
        <TapeDeckButton
          size="sm"
          variant="amber"
          disabled={disabled}
          className="sm:ml-auto"
          onClick={() =>
            apply(
              { monitor_option: option, apply_monitor_to_albums: applyToAlbums },
              `Set monitoring to "${MONITOR_OPTION_LABELS[option]}"`
            )
          }
        >
          Apply monitoring
        </TapeDeckButton>
      </div>

      <div className="flex flex-col sm:flex-row sm:items-center gap-2">
        <select
          value={profile}
          onChange={(e) => setProfile(e.target.value)}
          disabled={edit.busy || profilesLoading}
          aria-label="Quality profile"
          className={`${inputClass} sm:w-56`}
        >
          <option value="">{profilesLoading ? 'Loading profiles...' : 'Quality profile...'}</option>
          <option value={NO_PROFILE}>No profile (clear)</option>
          {profiles.map((p) => (
            <option key={p.id} value={String(p.id)}>
              {p.name}
            </option>
          ))}
        </select>
        <TapeDeckButton
          size="sm"
          disabled={disabled || profile === ''}
          className="sm:ml-auto"
          onClick={() => {
            const chosen = profile === NO_PROFILE ? null : profile;
            const name = chosen === null ? 'no profile' : (profiles.find((p) => String(p.id) === chosen)?.name ?? chosen);
            apply({ quality_profile_id: chosen }, `Set quality profile to ${name}`);
          }}
        >
          Apply profile
        </TapeDeckButton>
      </div>
    </MachinedCard>
  );
};
