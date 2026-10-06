import React from 'react';
import { Pencil, Trash2 } from 'lucide-react';
import { ConfirmDialog, MachinedCard, TapeDeckButton } from '@/components/ui';
import type { Schema } from '@/types';
import { useTagManager } from '@/hooks/useTagManager';
import { TagEditorModal } from './TagEditorModal';
import { Badge, EmptyNote, ProfileSection } from './ProfileSection';

const plural = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;

function usageSummary(tag: Schema<'TagOut'>): string {
  return [
    plural(tag.artist_count, 'artist'),
    plural(tag.delay_profile_count, 'delay profile'),
    plural(tag.release_profile_count, 'release profile'),
    plural(tag.import_list_count, 'import list'),
  ].join(' | ');
}

export interface TagsSectionProps {
  enabled: boolean;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Artist tags with usage counts: add, rename, and delete behind a confirm that states what it detaches. */
export const TagsSection: React.FC<TagsSectionProps> = ({ enabled, onToast }) => {
  const m = useTagManager(enabled, onToast);
  const { tags, loading, loadError } = m.catalogue;
  const del = m.pendingDelete;

  return (
    <ProfileSection
      title="Tags"
      hint="Tag artists, then scope delay profiles, release profiles and import lists to those tags."
      onAdd={m.openCreate}
    >
      {loadError && (
        <p role="alert" className="text-xs font-mono text-[var(--status-error)]">
          {loadError}
        </p>
      )}
      {loading && tags.length === 0 ? (
        <EmptyNote>Loading tags...</EmptyNote>
      ) : tags.length === 0 ? (
        <EmptyNote>No tags yet.</EmptyNote>
      ) : (
        <ul className="space-y-2" aria-label="Tags">
          {tags.map((t) => (
            <li key={t.id}>
              <MachinedCard className="flex items-center justify-between gap-2 p-2.5">
                <div className="min-w-0">
                  <Badge tone="amber">{t.label}</Badge>
                  <p className="mt-1 text-[11px] font-mono text-neutral-400">{usageSummary(t)}</p>
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  <TapeDeckButton size="sm" aria-label={`Rename ${t.label}`} onClick={() => m.openRename(t)} icon={<Pencil className="h-3.5 w-3.5" />} />
                  <TapeDeckButton
                    size="sm"
                    variant="danger"
                    aria-label={`Delete ${t.label}`}
                    onClick={() => m.askDelete(t)}
                    icon={<Trash2 className="h-3.5 w-3.5" />}
                  />
                </div>
              </MachinedCard>
            </li>
          ))}
        </ul>
      )}
      <TagEditorModal target={m.editing} onClose={m.closeEditor} onSave={m.saveLabel} />
      <ConfirmDialog
        isOpen={del !== null}
        title="Delete tag"
        confirmLabel="Delete"
        busy={m.deleting}
        onConfirm={() => void m.confirmDelete()}
        onCancel={m.cancelDelete}
      >
        {del && (
          <>
            <p className="font-mono text-xs text-white">{`Delete "${del.label}"?`}</p>
            <p className="mt-2 font-mono text-[11px] text-amber-300">
              {`Removes it from ${plural(del.artist_count, 'artist')} and ${plural(
                del.delay_profile_count + del.release_profile_count,
                'profile'
              )}`}
              {del.import_list_count > 0 ? ` (and ${plural(del.import_list_count, 'import list')})` : ''}.
            </p>
          </>
        )}
      </ConfirmDialog>
    </ProfileSection>
  );
};
