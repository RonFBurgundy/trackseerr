import { useCallback, useState } from 'react';
import type { Schema } from '@/types';
import { useTags, type UseTagsReturn } from './useTags';

export type TagEditorTarget = Schema<'TagOut'> | 'new';

export interface UseTagManagerReturn {
  catalogue: UseTagsReturn;
  /** The tag being renamed, 'new' for the create modal, null when closed. */
  editing: TagEditorTarget | null;
  openCreate: () => void;
  openRename: (tag: Schema<'TagOut'>) => void;
  closeEditor: () => void;
  /** Creates or renames; resolves an inline error message, or null on success. */
  saveLabel: (label: string) => Promise<string | null>;
  /** The tag awaiting delete confirmation (its counts feed the confirm text). */
  pendingDelete: Schema<'TagOut'> | null;
  askDelete: (tag: Schema<'TagOut'>) => void;
  cancelDelete: () => void;
  deleting: boolean;
  confirmDelete: () => Promise<void>;
}

/** Tag management for the Profiles page: create, rename and confirm-then-delete over `useTags`. */
export function useTagManager(enabled: boolean, onToast: (msg: string, tone?: 'ok' | 'error') => void): UseTagManagerReturn {
  const catalogue = useTags(enabled);
  const { create, rename, remove } = catalogue;
  const [editing, setEditing] = useState<TagEditorTarget | null>(null);
  const [pendingDelete, setPendingDelete] = useState<Schema<'TagOut'> | null>(null);
  const [deleting, setDeleting] = useState<boolean>(false);

  const openCreate = useCallback((): void => setEditing('new'), []);
  const openRename = useCallback((tag: Schema<'TagOut'>): void => setEditing(tag), []);
  const closeEditor = useCallback((): void => setEditing(null), []);
  const askDelete = useCallback((tag: Schema<'TagOut'>): void => setPendingDelete(tag), []);
  const cancelDelete = useCallback((): void => setPendingDelete(null), []);

  const saveLabel = useCallback(
    async (label: string): Promise<string | null> => {
      if (editing === null) return null;
      const res = editing === 'new' ? await create(label) : await rename(editing.id, label);
      if (!res.ok) return res.error;
      onToast(editing === 'new' ? `Tag "${res.tag.label}" created` : `Tag renamed to "${res.tag.label}"`);
      return null;
    },
    [editing, create, rename, onToast]
  );

  const confirmDelete = useCallback(async (): Promise<void> => {
    if (!pendingDelete) return;
    setDeleting(true);
    const err = await remove(pendingDelete.id);
    setDeleting(false);
    if (err) {
      onToast(err, 'error');
      return;
    }
    onToast(`Tag "${pendingDelete.label}" deleted`);
    setPendingDelete(null);
  }, [pendingDelete, remove, onToast]);

  return { catalogue, editing, openCreate, openRename, closeEditor, saveLabel, pendingDelete, askDelete, cancelDelete, deleting, confirmDelete };
}
