import React, { useState } from 'react';
import { Pencil } from 'lucide-react';
import { ObsidianModal, TagPicker, TapeDeckButton } from '@/components/ui';
import { useArtistTags } from '@/hooks/useArtistTags';
import { useTags } from '@/hooks/useTags';

export interface ArtistTagsRowProps {
  artistId: number | string;
  artistName: string;
  /** Tag ids currently on the artist. */
  tagIds: readonly number[];
  /** Called with the saved ids so the parent can patch its copy of the artist. */
  onSaved: (tagIds: number[]) => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Admin, native-mode only: a single row of the artist's tag chips and one key that opens the picker. */
export const ArtistTagsRow: React.FC<ArtistTagsRowProps> = ({ artistId, artistName, tagIds, onSaved, onToast }) => {
  const catalogue = useTags(true);
  const { saving, save } = useArtistTags(artistId, onToast);
  const [editing, setEditing] = useState<boolean>(false);
  const [draft, setDraft] = useState<number[]>([]);

  const labelOf = (id: number): string => catalogue.tags.find((t) => t.id === id)?.label ?? `#${id}`;

  const open = (): void => {
    setDraft([...tagIds]);
    setEditing(true);
  };

  const commit = async (): Promise<void> => {
    const saved = await save(draft);
    if (saved === null) return;
    onSaved(saved);
    setEditing(false);
  };

  return (
    <div className="flex min-w-0 items-center gap-1.5">
      <ul
        aria-label="Artist tags"
        className="flex min-w-0 flex-1 flex-nowrap items-center gap-1.5 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >
        {tagIds.length === 0 ? (
          <li className="text-[11px] font-mono text-[var(--text-muted)]">No tags</li>
        ) : (
          tagIds.map((id) => (
            <li
              key={id}
              className="inline-flex h-[22px] shrink-0 items-center whitespace-nowrap rounded-[2px] border border-[var(--border-default)] bg-[var(--bg-surface-elevated)] px-1.5 text-[11px] font-mono text-[var(--text-secondary)]"
            >
              {labelOf(id)}
            </li>
          ))
        )}
      </ul>
      <TapeDeckButton
        size="sm"
        className="!h-8 !min-h-8 !w-8 shrink-0 !p-0"
        aria-label="Edit tags"
        title="Edit tags"
        onClick={open}
        icon={<Pencil className="h-3.5 w-3.5" />}
      />
      <ObsidianModal
        isOpen={editing}
        onClose={() => setEditing(false)}
        title="Edit Tags"
        subtitle={artistName}
        maxWidth="sm:max-w-md"
        footer={
          <>
            <TapeDeckButton variant="amber" disabled={saving} onClick={() => void commit()}>
              {saving ? 'Saving...' : 'Save'}
            </TapeDeckButton>
            <TapeDeckButton disabled={saving} onClick={() => setEditing(false)}>
              Cancel
            </TapeDeckButton>
          </>
        }
      >
        <TagPicker
          mode="id"
          label="Tags"
          name="artist_tags"
          tags={catalogue.tags}
          loading={catalogue.loading}
          loadError={catalogue.loadError}
          onCreate={catalogue.create}
          disabled={saving}
          value={draft}
          onChange={setDraft}
          hint="Tags scope delay and release profiles to this artist."
        />
      </ObsidianModal>
    </div>
  );
};
