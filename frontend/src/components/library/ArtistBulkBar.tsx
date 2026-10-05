import React, { useId, useState } from 'react';
import { Check, Eye, EyeOff, Loader2, X } from 'lucide-react';
import type { QualityProfile } from '@/types/models';
import type { ReleaseProfile } from '@/types/releaseProfiles';
import { MONITOR_OPTION_HINTS, MONITOR_OPTION_LABELS, type MonitorOption } from '@/types/monitoring';
import type { UseBulkSelectionReturn } from '@/hooks/useBulkSelection';
import type { UseArtistBulkEditReturn } from '@/hooks/useArtistBulkEdit';
import { ActionBar, MachinedCard, MonitorOptionSelect, TapeDeckButton } from '@/components/ui';
import { inputClass } from '@/components/settings/formClasses';

const NO_PROFILE = '__none__';
const KEEP = '';

export interface ArtistBulkBarProps {
  selection: UseBulkSelectionReturn;
  /** Artists in the current list (what "All N artists" counts). */
  total: number;
  /** False while a search or monitored-only filter is on: server-side "all" ignores filters. */
  canSelectAll: boolean;
  edit: UseArtistBulkEditReturn;
  profiles: QualityProfile[];
  profilesLoading: boolean;
  /** Native release profiles for the "Release profile" select (optional, shape automatic monitoring only). */
  releaseProfiles: ReleaseProfile[];
  releaseProfilesLoading: boolean;
}

/** Lidarr-style mass editor for the artists grid. */
export const ArtistBulkBar: React.FC<ArtistBulkBarProps> = ({
  selection,
  total,
  canSelectAll,
  edit,
  profiles,
  profilesLoading,
  releaseProfiles,
  releaseProfilesLoading,
}) => {
  const uid = useId();
  const [option, setOption] = useState<MonitorOption>('existing');
  const [applyToAlbums, setApplyToAlbums] = useState<boolean>(true);
  const [profile, setProfile] = useState<string>('');
  const [releaseProfile, setReleaseProfile] = useState<string>(KEEP);

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
        {pending.patch.release_profile_id !== undefined && (
          <p className="text-[11px] font-mono text-amber-300">
            {pending.patch.apply_monitor_to_albums === true
              ? `"Also apply to existing albums" is checked: this will recompute monitoring for ${pending.targetCount.toLocaleString()} ${pending.targetCount === 1 ? 'artist' : 'artists'} and replace manual monitoring choices.`
              : `"Also apply to existing albums" is unchecked: the profile is saved for future releases only; existing albums are unchanged.`}
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
          onClick={() => apply({ monitored: true, apply_monitor_to_albums: applyToAlbums }, 'Monitor')}
          icon={<Eye className="h-3.5 w-3.5" />}
        >
          Monitor
        </TapeDeckButton>
        <TapeDeckButton
          size="sm"
          disabled={disabled}
          onClick={() => apply({ monitored: false, apply_monitor_to_albums: applyToAlbums }, 'Unmonitor')}
          icon={<EyeOff className="h-3.5 w-3.5" />}
        >
          Unmonitor
        </TapeDeckButton>
      </ActionBar>

      <div className="flex flex-col sm:flex-row sm:flex-wrap sm:items-center gap-2">
        <MonitorOptionSelect
          value={option}
          onChange={setOption}
          id={`${uid}-option`}
          name="monitor-option"
          disabled={edit.busy}
          aria-label="Monitor option"
          className="sm:w-56"
        />
        <select
          id={`${uid}-release-profile`}
          name="release-profile"
          value={releaseProfile}
          onChange={(e) => setReleaseProfile(e.target.value)}
          disabled={edit.busy || releaseProfilesLoading}
          aria-label="Release profile"
          className={`${inputClass} sm:w-56`}
        >
          <option value={KEEP}>{releaseProfilesLoading ? 'Loading profiles...' : 'Release profile: unchanged'}</option>
          <option value={NO_PROFILE}>No release profile (clear)</option>
          {releaseProfiles.map((p) => (
            <option key={p.id} value={String(p.id)}>
              {p.name}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-2 min-h-[44px] sm:min-h-0 text-xs font-mono text-neutral-300 cursor-pointer">
          <input
            id={`${uid}-albums`}
            name="apply-to-albums"
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
          onClick={() => {
            const patch: Parameters<UseArtistBulkEditReturn['request']>[0] = {
              monitor_option: option,
              apply_monitor_to_albums: applyToAlbums,
            };
            let summary = `Set monitoring to "${MONITOR_OPTION_LABELS[option]}"`;
            if (releaseProfile !== KEEP) {
              const chosen = releaseProfile === NO_PROFILE ? null : Number(releaseProfile);
              patch.release_profile_id = chosen;
              const name = chosen === null ? 'no release profile' : (releaseProfiles.find((p) => p.id === chosen)?.name ?? String(chosen));
              summary += ` with ${name}`;
            }
            apply(patch, summary);
          }}
        >
          Apply monitoring
        </TapeDeckButton>
      </div>
      <p className="text-[11px] font-mono text-neutral-500">{MONITOR_OPTION_HINTS[option]}</p>

      <div className="flex flex-col sm:flex-row sm:items-center gap-2">
        <select
          id={`${uid}-profile`}
          name="quality-profile"
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
