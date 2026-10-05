import React, { useEffect, useId, useState } from 'react';
import { X } from 'lucide-react';
import type { QualityProfile } from '@/types/qualityProfiles';
import type { MetadataProfile } from '@/types/metadataProfiles';
import { MONITOR_OPTIONS, MONITOR_OPTION_HINTS, MONITOR_OPTION_LABELS, type MonitorOption } from '@/types/monitoring';
import type { UseBulkSelectionReturn } from '@/hooks/useBulkSelection';
import type { ArtistBulkPatch, UseArtistBulkEditReturn } from '@/hooks/useArtistBulkEdit';
import { ConfirmDialog, TapeDeckButton } from '@/components/ui';
import { BulkEditSheet, BulkField, bulkSelectClass } from './BulkEditSheet';

/** Sentinel for "No change" in every field. */
const KEEP = '';
/** Sentinel for an explicit clear (profile = none). */
const NONE = '__none__';

type MonitoredChoice = '' | 'true' | 'false';

function isMonitoredChoice(v: string): v is MonitoredChoice {
  return v === '' || v === 'true' || v === 'false';
}

function isMonitorOption(v: string): v is MonitorOption {
  return MONITOR_OPTIONS.some((o) => o.value === v);
}

export interface ArtistBulkBarProps {
  selection: UseBulkSelectionReturn;
  /** Artists in the current list (what "All N artists" counts). */
  total: number;
  /** False while a search or monitored-only filter is on: server-side "all" ignores filters. */
  canSelectAll: boolean;
  edit: UseArtistBulkEditReturn;
  profiles: QualityProfile[];
  profilesLoading: boolean;
  /** Native metadata profiles for the "Metadata profile" select (optional, shape automatic monitoring only). */
  metadataProfiles: MetadataProfile[];
  metadataProfilesLoading: boolean;
}

/** Lidarr-style mass editor for the artists grid: choose the changes, then press Apply once. */
export const ArtistBulkBar: React.FC<ArtistBulkBarProps> = ({
  selection,
  total,
  canSelectAll,
  edit,
  profiles,
  profilesLoading,
  metadataProfiles,
  metadataProfilesLoading,
}) => {
  const uid = useId();
  const [monitored, setMonitored] = useState<MonitoredChoice>('');
  const [option, setOption] = useState<string>(KEEP);
  const [quality, setQuality] = useState<string>(KEEP);
  const [metadataProfile, setMetadataProfile] = useState<string>(KEEP);
  const [applyToAlbums, setApplyToAlbums] = useState<boolean>(false);
  // Until the user touches the checkbox it mirrors the server default; only a touched value is sent.
  const [applyTouched, setApplyTouched] = useState<boolean>(false);

  const count = selection.count(total);
  const empty = count === 0;
  const countLabel = selection.allMatching ? `All ${total.toLocaleString()} artists` : `${count.toLocaleString()} selected`;
  const dirty = monitored !== '' || option !== KEEP || quality !== KEEP || metadataProfile !== KEEP;
  const cascadeRelevant = monitored !== '' || option !== KEEP || metadataProfile !== KEEP;

  // Server default: cascade on unmonitor, otherwise recompute only artists whose monitor option changes.
  const serverDefaultApply = monitored === 'false';
  useEffect(() => {
    if (!applyTouched) setApplyToAlbums(serverDefaultApply);
  }, [applyTouched, serverDefaultApply]);

  const resetFields = (): void => {
    setMonitored('');
    setOption(KEEP);
    setQuality(KEEP);
    setMetadataProfile(KEEP);
    setApplyToAlbums(false);
    setApplyTouched(false);
  };

  const qualityName = (v: string): string =>
    v === NONE ? 'no quality profile' : (profiles.find((p) => String(p.id) === v)?.name ?? v);
  const releaseName = (v: string): string =>
    v === NONE ? 'no metadata profile' : (metadataProfiles.find((p) => String(p.id) === v)?.name ?? v);

  const stage = (): void => {
    const patch: ArtistBulkPatch = {};
    const changes: string[] = [];
    const warnings: string[] = [];
    if (monitored !== '') {
      patch.monitored = monitored === 'true';
      changes.push(monitored === 'true' ? 'Monitored' : 'Unmonitored');
    }
    if (isMonitorOption(option)) {
      patch.monitor_option = option;
      changes.push(`monitor option ${MONITOR_OPTION_LABELS[option]}`);
    }
    if (quality !== KEEP) {
      patch.quality_profile_id = quality === NONE ? null : quality;
      changes.push(`quality profile ${qualityName(quality)}`);
    }
    if (metadataProfile !== KEEP) {
      patch.metadata_profile_id = metadataProfile === NONE ? null : Number(metadataProfile);
      changes.push(`metadata profile ${releaseName(metadataProfile)}`);
    }
    if (cascadeRelevant) {
      if (applyTouched) {
        patch.apply_monitor_to_albums = applyToAlbums;
        if (applyToAlbums) {
          changes.push('apply to their existing albums (replaces manual choices)');
          warnings.push('Monitoring on every existing album of each artist is recomputed; manual album choices are replaced.');
        } else {
          warnings.push('Existing albums are left unchanged; settings apply to future releases only.');
        }
      } else if (monitored === 'false') {
        warnings.push('Unmonitoring also unmonitors every existing album of these artists.');
      } else if (option !== KEEP) {
        warnings.push('Existing albums are recomputed only for artists whose monitor option actually changes; manual choices on those are replaced.');
      } else {
        warnings.push('Existing albums are unchanged; settings apply to future releases only.');
      }
    }
    const target = `${count.toLocaleString()} ${count === 1 ? 'artist' : 'artists'}`;
    const lead = monitored !== '' ? `Set ${target} to ${changes[0]}` : `Set ${target}: ${changes[0]}`;
    const rest = changes.slice(1);
    const summary =
      rest.length === 0
        ? lead
        : `${lead}, ${rest.length > 1 ? `${rest.slice(0, -1).join(', ')}, and ${rest[rest.length - 1]}` : `and ${rest[0]}`}`;
    edit.request(patch, summary, changes.join(', '), warnings);
  };

  const handleConfirm = async (): Promise<void> => {
    if (await edit.confirm()) resetFields();
  };

  const field = (suffix: string): string => `${uid}-${suffix}`;

  return (
    <>
      <BulkEditSheet
        ariaLabel="Bulk edit artists"
        countLabel={countLabel}
        applyDisabled={empty || !dirty}
        busy={edit.busy}
        onApply={stage}
        headerActions={
          <>
            {canSelectAll && !selection.allMatching && (
              <TapeDeckButton type="button" size="sm" onClick={selection.selectAllMatching}>
                {`All ${total.toLocaleString()}`}
              </TapeDeckButton>
            )}
            <TapeDeckButton type="button" size="sm" onClick={selection.clear} disabled={empty}>
              Clear selection
            </TapeDeckButton>
            <TapeDeckButton type="button" size="sm" onClick={selection.exit} icon={<X className="h-3.5 w-3.5" />}>
              Done
            </TapeDeckButton>
          </>
        }
        notice={
          <>
            {isMonitorOption(option) && (
              <p className="text-[11px] font-mono text-neutral-500">{MONITOR_OPTION_HINTS[option]}</p>
            )}
            {!canSelectAll && (
              <p className="text-[11px] font-mono text-neutral-500">Clear search and filters to select every artist.</p>
            )}
          </>
        }
      >
        <BulkField id={field('monitored')} label="Monitored">
          <select
            id={field('monitored')}
            name="bulk-monitored"
            value={monitored}
            disabled={edit.busy}
            onChange={(e) => {
              if (isMonitoredChoice(e.target.value)) setMonitored(e.target.value);
            }}
            className={bulkSelectClass}
          >
            <option value="">No change</option>
            <option value="true">Monitored</option>
            <option value="false">Unmonitored</option>
          </select>
        </BulkField>
        <BulkField id={field('option')} label="Monitor option">
          <select
            id={field('option')}
            name="bulk-monitor-option"
            value={option}
            disabled={edit.busy}
            onChange={(e) => setOption(e.target.value)}
            className={bulkSelectClass}
          >
            <option value={KEEP}>No change</option>
            {MONITOR_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </BulkField>
        <BulkField id={field('quality')} label="Quality profile">
          <select
            id={field('quality')}
            name="bulk-quality-profile"
            value={quality}
            disabled={edit.busy || profilesLoading}
            onChange={(e) => setQuality(e.target.value)}
            className={bulkSelectClass}
          >
            <option value={KEEP}>{profilesLoading ? 'Loading...' : 'No change'}</option>
            <option value={NONE}>None</option>
            {profiles.map((p) => (
              <option key={p.id} value={String(p.id)}>
                {p.name}
              </option>
            ))}
          </select>
        </BulkField>
        <BulkField id={field('metadata')} label="Metadata profile">
          <select
            id={field('metadata')}
            name="bulk-metadata-profile"
            value={metadataProfile}
            disabled={edit.busy || metadataProfilesLoading}
            onChange={(e) => setMetadataProfile(e.target.value)}
            className={bulkSelectClass}
          >
            <option value={KEEP}>{metadataProfilesLoading ? 'Loading...' : 'No change'}</option>
            <option value={NONE}>None</option>
            {metadataProfiles.map((p) => (
              <option key={p.id} value={String(p.id)}>
                {p.name}
              </option>
            ))}
          </select>
        </BulkField>
        {cascadeRelevant && (
          <label
            htmlFor={field('albums')}
            className="col-span-2 flex min-h-[36px] cursor-pointer items-center gap-2 text-xs font-mono text-neutral-300 sm:col-span-1 sm:min-h-0"
          >
            <input
              id={field('albums')}
              name="bulk-apply-to-albums"
              type="checkbox"
              checked={applyToAlbums}
              disabled={edit.busy}
              onChange={(e) => {
                setApplyTouched(true);
                setApplyToAlbums(e.target.checked);
              }}
              className="h-5 w-5 accent-[#e5a00d]"
            />
            Also apply to existing albums
          </label>
        )}
      </BulkEditSheet>
      <ConfirmDialog
        isOpen={edit.pending !== null}
        title="Apply bulk edit"
        confirmLabel="Apply"
        busy={edit.busy}
        onConfirm={() => void handleConfirm()}
        onCancel={edit.cancel}
      >
        <p className="font-mono text-xs text-white">{edit.pending?.summary}</p>
        {edit.pending?.warnings.map((w) => (
          <p key={w} className="mt-2 font-mono text-[11px] text-amber-300">
            {w}
          </p>
        ))}
      </ConfirmDialog>
    </>
  );
};
