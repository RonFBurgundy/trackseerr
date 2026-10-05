import React, { useId, useState } from 'react';
import { Save, Trash2, Loader2 } from 'lucide-react';
import type { PlexMix, PlexMixSnapshot } from '@/types/models';
import { MachinedCard, TapeDeckButton, TactileSwitch } from '@/components/ui';

export interface PlexMixesPanelProps {
  mixes: PlexMix[];
  snapshots: PlexMixSnapshot[];
  isLoading: boolean;
  isMutating: boolean;
  onSave: (mix: PlexMix, title: string, autoRefresh: boolean) => Promise<boolean>;
  onToggleRefresh: (snapshot: PlexMixSnapshot, autoRefresh: boolean) => Promise<void>;
  onRemove: (snapshot: PlexMixSnapshot) => Promise<void>;
}

const INPUT =
  'w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]';

const MixCard: React.FC<{
  mix: PlexMix;
  isMutating: boolean;
  onSave: PlexMixesPanelProps['onSave'];
}> = ({ mix, isMutating, onSave }) => {
  const uid = useId();
  const [title, setTitle] = useState<string>(`${mix.title} (Saved)`);
  const [autoRefresh, setAutoRefresh] = useState<boolean>(false);

  return (
    <MachinedCard className="p-4 space-y-3">
      <div>
        <p className="text-[10px] uppercase tracking-wider text-neutral-500 font-mono truncate">{mix.hub_title}</p>
        <h4 className="font-bold text-base text-white truncate" title={mix.title}>
          {mix.title}
        </h4>
        {mix.track_count !== null && (
          <p className="text-xs text-neutral-400 font-mono mt-1">{mix.track_count} tracks</p>
        )}
      </div>
      <input
        id={`${uid}-title`}
        name="playlist-title"
        type="text"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        className={INPUT}
        aria-label={`Playlist title for ${mix.title}`}
      />
      <TactileSwitch checked={autoRefresh} onChange={setAutoRefresh} label="Auto-refresh" ariaLabel={`Auto-refresh ${mix.title}`} />
      <TapeDeckButton
        size="sm"
        variant="amber"
        disabled={isMutating || !title.trim()}
        onClick={() => void onSave(mix, title, autoRefresh)}
        icon={<Save className="h-3.5 w-3.5" />}
      >
        {mix.snapshot_id ? 'Replace playlist' : 'Save as playlist'}
      </TapeDeckButton>
    </MachinedCard>
  );
};

export const PlexMixesPanel: React.FC<PlexMixesPanelProps> = ({
  mixes,
  snapshots,
  isLoading,
  isMutating,
  onSave,
  onToggleRefresh,
  onRemove,
}) => {
  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <section className="space-y-3">
        <h3 className="text-xs uppercase tracking-widest text-neutral-300 font-mono">Plexamp Mixes</h3>
        {mixes.length === 0 ? (
          <div className="text-center py-10 text-neutral-500 font-mono text-sm">No mixes available.</div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {mixes.map((mix) => (
              <MixCard key={mix.mix_key} mix={mix} isMutating={isMutating} onSave={onSave} />
            ))}
          </div>
        )}
      </section>

      <section className="space-y-3">
        <h3 className="text-xs uppercase tracking-widest text-neutral-300 font-mono">Saved Snapshots</h3>
        {snapshots.length === 0 ? (
          <div className="text-center py-6 text-neutral-500 font-mono text-sm">No saved mixes yet.</div>
        ) : (
          <div className="space-y-2">
            {snapshots.map((s) => (
              <MachinedCard key={s.id} className="p-3 flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm text-white font-bold truncate">{s.playlist_title}</p>
                  <p className="text-[11px] text-neutral-500 font-mono truncate">
                    From {s.mix_title}
                    {s.last_refreshed_at ? ` · refreshed ${new Date(s.last_refreshed_at).toLocaleString()}` : ''}
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  <TactileSwitch
                    checked={s.auto_refresh}
                    onChange={(v) => void onToggleRefresh(s, v)}
                    label="Auto-refresh"
                    ariaLabel={`Auto-refresh ${s.playlist_title}`}
                  />
                  <TapeDeckButton
                    size="sm"
                    variant="danger"
                    aria-label="Remove snapshot"
                    title="Removes the snapshot record only; the Plex playlist is kept."
                    onClick={() => void onRemove(s)}
                    icon={<Trash2 className="h-3.5 w-3.5" />}
                  />
                </div>
              </MachinedCard>
            ))}
          </div>
        )}
      </section>
    </div>
  );
};
