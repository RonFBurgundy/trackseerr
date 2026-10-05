import React, { useId, useState } from 'react';
import { Loader2, Play, Eye, Trash2 } from 'lucide-react';
import type {
  MixConfig,
  MixConfigUpdateBody,
  MixTrack,
  MixTrackStatus,
  TailoredMixResult,
} from '@/types/models';
import { MachinedCard, TapeDeckButton, TactileSwitch } from '@/components/ui';

const TYPE_LABEL: Record<MixConfig['mix_type'], string> = {
  discover_weekly: 'Discover Weekly',
  daily_blend: 'Daily Blend',
  artist_radio: 'Artist Radio',
};

const STATUS_COLOR: Record<MixTrackStatus, string> = {
  available: 'text-[#22c55e]',
  queued: 'text-[#e5a00d]',
  missing: 'text-neutral-500',
};

const OriginBadge: React.FC<{ origin: MixTrack['origin'] }> = ({ origin }) => (
  <span
    className={`text-[10px] font-mono uppercase px-1.5 py-0.5 rounded-[3px] border ${
      origin === 'discovery' ? 'border-[#e5a00d]/50 text-[#e5a00d]' : 'border-[#2a2a2a] text-neutral-400'
    }`}
  >
    {origin}
  </span>
);

export interface MixCardProps {
  mix: MixConfig;
  result: TailoredMixResult | undefined;
  preview: MixTrack[] | undefined;
  isBusy: boolean;
  isGenerating: boolean;
  onUpdate: (id: string, body: MixConfigUpdateBody) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  onPreview: (id: string) => Promise<void>;
  onGenerate: (id: string) => Promise<void>;
}

export const MixCard: React.FC<MixCardProps> = ({
  mix,
  result,
  preview,
  isBusy,
  isGenerating,
  onUpdate,
  onDelete,
  onPreview,
  onGenerate,
}) => {
  const uid = useId();
  const [ratio, setRatio] = useState<number>(Math.round(mix.discovery_ratio * 100));
  const [confirmDelete, setConfirmDelete] = useState<boolean>(false);

  return (
    <MachinedCard className="p-4 space-y-4 w-full">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] font-mono uppercase tracking-wider text-[#e5a00d]">
            {TYPE_LABEL[mix.mix_type]}
          </div>
          <div className="text-base font-bold text-white truncate">{mix.name}</div>
          {mix.seed_artist && <div className="text-xs text-neutral-400 truncate">Seed: {mix.seed_artist}</div>}
        </div>
        <TactileSwitch
          checked={mix.enabled}
          disabled={isBusy}
          onChange={(v) => void onUpdate(mix.id, { enabled: v })}
          title="Enabled"
          ariaLabel={`Enable mix ${mix.name}`}
        />
      </div>

      <div className="text-xs font-mono text-neutral-400">{mix.track_count} tracks</div>

      <div>
        <label
          htmlFor={`${uid}-ratio`}
          className="flex items-center justify-between text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5"
        >
          <span>Familiar hits ↔ Deep discoveries</span>
          <span className="text-[#e5a00d]">{ratio}%</span>
        </label>
        <input
          id={`${uid}-ratio`}
          name="discovery-ratio"
          type="range"
          min={0}
          max={100}
          step={5}
          value={ratio}
          disabled={isBusy}
          onChange={(e) => setRatio(Number(e.target.value))}
          onPointerUp={() => void onUpdate(mix.id, { discovery_ratio: ratio / 100 })}
          onKeyUp={() => void onUpdate(mix.id, { discovery_ratio: ratio / 100 })}
          className="w-full h-11 sm:h-6 accent-[#e5a00d]"
        />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label
            htmlFor={`${uid}-seed-window`}
            className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5"
          >
            Seed window (days)
          </label>
          <input
            id={`${uid}-seed-window`}
            name="seed-window-days"
            type="number"
            min={1}
            max={90}
            defaultValue={mix.seed_window_days}
            disabled={isBusy}
            onBlur={(e) => {
              const v = Math.min(90, Math.max(1, Number(e.target.value) || mix.seed_window_days));
              if (v !== mix.seed_window_days) void onUpdate(mix.id, { seed_window_days: v });
            }}
            className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]"
          />
        </div>
        <div>
          <label
            htmlFor={`${uid}-weekly-quota`}
            className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5"
          >
            Weekly acquisition quota
          </label>
          <input
            id={`${uid}-weekly-quota`}
            name="max-weekly-acquisitions"
            type="number"
            min={0}
            max={100}
            defaultValue={mix.max_weekly_acquisitions}
            disabled={isBusy || !mix.auto_acquire_missing}
            onBlur={(e) => {
              const v = Math.min(100, Math.max(0, Number(e.target.value) || 0));
              if (v !== mix.max_weekly_acquisitions) void onUpdate(mix.id, { max_weekly_acquisitions: v });
            }}
            className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d] disabled:opacity-50"
          />
        </div>
      </div>

      <TactileSwitch
        checked={mix.auto_acquire_missing}
        disabled={isBusy}
        onChange={(v) => void onUpdate(mix.id, { auto_acquire_missing: v })}
        label="Auto-acquire missing"
      />

      <div className="flex flex-wrap items-center gap-2">
        <TapeDeckButton size="sm" disabled={isBusy} onClick={() => void onPreview(mix.id)} icon={<Eye className="h-3.5 w-3.5" />}>
          Preview
        </TapeDeckButton>
        <TapeDeckButton
          size="sm"
          variant="amber"
          disabled={isGenerating}
          onClick={() => void onGenerate(mix.id)}
          icon={isGenerating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
        >
          {isGenerating ? 'Generating...' : 'Generate & sync to Plexamp'}
        </TapeDeckButton>
        {confirmDelete ? (
          <>
            <TapeDeckButton
              size="sm"
              variant="danger"
              disabled={isBusy}
              onClick={() => {
                setConfirmDelete(false);
                void onDelete(mix.id);
              }}
            >
              Confirm Delete
            </TapeDeckButton>
            <TapeDeckButton size="sm" onClick={() => setConfirmDelete(false)}>
              Cancel
            </TapeDeckButton>
          </>
        ) : (
          <TapeDeckButton
            size="sm"
            variant="danger"
            disabled={isBusy}
            onClick={() => setConfirmDelete(true)}
            aria-label="Delete mix"
            icon={<Trash2 className="h-3.5 w-3.5" />}
          />
        )}
      </div>

      {result && (
        <div className="border-t border-[#222222] pt-3 space-y-1 text-xs font-mono text-neutral-300">
          <div className="text-neutral-500">Last run: {new Date(result.generated_at).toLocaleString()}</div>
          <div>
            {result.total} tracks, {result.available} available, {result.missing} missing,{' '}
            {result.acquisitions_queued} queued, quota left {result.quota_remaining}
          </div>
          <div className={result.synced ? 'text-[#22c55e]' : 'text-[#ef4444]'}>
            {result.synced ? 'Synced to Plex' : `Not synced${result.sync_error ? `: ${result.sync_error}` : ''}`}
          </div>
        </div>
      )}

      {preview && (
        <div className="border-t border-[#222222] pt-3">
          <div className="text-xs uppercase font-mono text-neutral-400 mb-2">Preview ({preview.length})</div>
          <ul className="max-h-64 overflow-y-auto divide-y divide-[#222222]">
            {preview.map((t, i) => (
              <li key={`${t.artist}-${t.title}-${i}`} className="py-1.5 flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-sm text-white truncate">{t.title}</div>
                  <div className="text-xs text-neutral-400 truncate">{t.artist}</div>
                </div>
                <OriginBadge origin={t.origin} />
              </li>
            ))}
          </ul>
        </div>
      )}

      {!preview && result && result.tracks.length > 0 && (
        <details className="border-t border-[#222222] pt-3">
          <summary className="text-xs uppercase font-mono text-neutral-400 cursor-pointer min-h-[44px] sm:min-h-0 flex items-center">
            Last result tracks
          </summary>
          <ul className="max-h-64 overflow-y-auto divide-y divide-[#222222] mt-2">
            {result.tracks.map((t, i) => (
              <li key={`${t.artist}-${t.title}-${i}`} className="py-1.5 flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-sm text-white truncate">{t.title}</div>
                  <div className="text-xs text-neutral-400 truncate">{t.artist}</div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <OriginBadge origin={t.origin} />
                  <span className={`text-[10px] font-mono uppercase ${STATUS_COLOR[t.status]}`}>{t.status}</span>
                </div>
              </li>
            ))}
          </ul>
        </details>
      )}
    </MachinedCard>
  );
};
