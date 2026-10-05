import React, { useId, useState } from 'react';
import type { MixConfigCreateBody, MixType } from '@/types/models';
import { MachinedCard, TapeDeckButton, TabStrip } from '@/components/ui';

const TYPES: Array<{ id: MixType; label: string; defaultName: string }> = [
  { id: 'discover_weekly', label: 'Discover Weekly', defaultName: 'Discover Weekly · TrackSeerr' },
  { id: 'daily_blend', label: 'Daily Blend', defaultName: 'Daily Blend · TrackSeerr' },
  { id: 'artist_radio', label: 'Artist Radio', defaultName: '' },
];

const inputClass =
  'w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]';
const labelClass = 'block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5';

export interface NewMixFormProps {
  onCreate: (body: MixConfigCreateBody) => Promise<boolean>;
  onCancel: () => void;
}

export const NewMixForm: React.FC<NewMixFormProps> = ({ onCreate, onCancel }) => {
  const uid = useId();
  const [mixType, setMixType] = useState<MixType>('discover_weekly');
  const [name, setName] = useState<string>('');
  const [seedArtist, setSeedArtist] = useState<string>('');
  const [trackCount, setTrackCount] = useState<number>(30);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);

  const isRadio = mixType === 'artist_radio';
  const canSubmit = !isSubmitting && (!isRadio || seedArtist.trim().length > 0);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    const def = TYPES.find((t) => t.id === mixType)?.defaultName ?? '';
    const finalName = name.trim() || (isRadio ? `${seedArtist.trim()} Radio · TrackSeerr` : def);
    setIsSubmitting(true);
    try {
      const ok = await onCreate({
        mix_type: mixType,
        name: finalName,
        seed_artist: isRadio ? seedArtist.trim() : null,
        track_count: Math.min(100, Math.max(5, trackCount)),
      });
      if (ok) onCancel();
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <MachinedCard className="p-3 sm:p-6 max-w-2xl">
      <form onSubmit={handleSubmit} className="space-y-4">
        <h3 className="text-sm font-bold uppercase tracking-wider text-white">New Mix</h3>
        <TabStrip fill>
          {TYPES.map((t) => (
            <TapeDeckButton
              key={t.id}
              type="button"
              size="sm"
              active={mixType === t.id}
              onClick={() => setMixType(t.id)}
            >
              {t.label}
            </TapeDeckButton>
          ))}
        </TabStrip>
        {isRadio && (
          <div>
            <label htmlFor={`${uid}-seed`} className={labelClass}>Seed artist</label>
            <input
              id={`${uid}-seed`}
              name="seed-artist"
              type="text"
              value={seedArtist}
              onChange={(e) => setSeedArtist(e.target.value)}
              className={inputClass}
              required
            />
          </div>
        )}
        <div>
          <label htmlFor={`${uid}-name`} className={labelClass}>Name (optional)</label>
          <input id={`${uid}-name`} name="mix-name" type="text" value={name} onChange={(e) => setName(e.target.value)} className={inputClass} />
        </div>
        <div>
          <label htmlFor={`${uid}-count`} className={labelClass}>Track count (5-100)</label>
          <input
            id={`${uid}-count`}
            name="track-count"
            type="number"
            min={5}
            max={100}
            value={trackCount}
            onChange={(e) => setTrackCount(Number(e.target.value) || 30)}
            className={inputClass}
          />
        </div>
        <div className="flex gap-2">
          <TapeDeckButton type="submit" variant="amber" disabled={!canSubmit}>
            Create Mix
          </TapeDeckButton>
          <TapeDeckButton type="button" onClick={onCancel}>
            Cancel
          </TapeDeckButton>
        </div>
      </form>
    </MachinedCard>
  );
};
