import React, { useEffect, useMemo } from 'react';
import { Loader2 } from 'lucide-react';
import type { User } from '@/types/models';
import type { LibraryFilters } from '@/types/libraryFilters';
import { SMART_SORT_LABELS, type SmartSortOption } from '@/types/smartCollections';
import { ObsidianModal, TapeDeckButton, FormField, inputClass } from '@/components/ui';
import { LibraryFilterFields } from '@/components/library/LibraryFilterFields';
import { useLibraryFacets } from '@/hooks/useLibraryFacets';
import { useTags } from '@/hooks/useTags';
import { useSmartCollectionEditor } from '@/hooks/useSmartCollectionEditor';

export interface SmartCollectionModalProps {
  open: boolean;
  onClose: () => void;
  onSaved?: () => void;
  collectionId?: string;
  initialFilters?: LibraryFilters;
  users?: User[];
  currentUserId?: string;
  canTargetUsers?: boolean;
  serverLabel?: string;
}

export const SmartCollectionModal: React.FC<SmartCollectionModalProps> = ({
  open,
  onClose,
  onSaved,
  collectionId,
  initialFilters,
  users = [],
  currentUserId,
  canTargetUsers = false,
  serverLabel = 'Plex',
}) => {
  const defaultTargets = useMemo(
    () => (currentUserId ? [String(currentUserId)] : []),
    [currentUserId]
  );

  const editor = useSmartCollectionEditor({
    collectionId,
    initialFilters,
    defaultTargets,
    enabled: open,
  });

  const { facets } = useLibraryFacets(open);
  const { tags } = useTags(open);
  const catalogEmpty =
    facets !== null &&
    [facets.genres, facets.decades, facets.countries, facets.album_types, facets.artist_types].every(
      (list) => list.every((f) => f.count <= 0)
    ) &&
    tags.length === 0;

  useEffect(() => {
    if (!open) {
      editor.reset();
    }
  }, [open, editor.reset]);

  const handleSave = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    try {
      await editor.save();
      onSaved?.();
      onClose();
    } catch {
      // editor.error is set by save()
    }
  };

  const previewCap = editor.preview.count > editor.limit;
  const matchDisplayCount = Math.min(editor.preview.count, editor.limit);

  return (
    <ObsidianModal
      isOpen={open}
      onClose={onClose}
      title={collectionId ? 'Edit Smart Collection' : 'Create Smart Collection'}
      subtitle="A playlist powered by saved library filters"
      maxWidth="sm:max-w-2xl"
      footer={
        <div className="flex items-center justify-end gap-2 w-full">
          <TapeDeckButton size="sm" onClick={onClose} disabled={editor.saving}>
            Cancel
          </TapeDeckButton>
          <TapeDeckButton
            size="sm"
            variant="amber"
            onClick={() => void handleSave()}
            disabled={
              editor.saving ||
              !editor.name.trim() ||
              editor.preview.count === 0
            }
            icon={editor.saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
          >
            {editor.saving ? 'Saving...' : collectionId ? 'Save' : 'Create'}
          </TapeDeckButton>
        </div>
      }
    >
      <form onSubmit={(e) => void handleSave(e)} className="space-y-5">
        {editor.error && (
          <div
            role="alert"
            className="p-2.5 rounded-[4px] bg-red-950/40 border border-red-800/50 text-xs font-mono text-red-300"
          >
            {editor.error}
          </div>
        )}

        {/* 1. Name and Description */}
        <div className="space-y-3">
          <FormField label="Collection Name" htmlFor="smart_collection_name">
            <input
              id="smart_collection_name"
              name="name"
              type="text"
              required
              autoComplete="off"
              placeholder="e.g. 90s Alternative Rock"
              value={editor.name}
              onChange={(e) => editor.setName(e.target.value)}
              className={inputClass}
            />
          </FormField>

          <FormField label="Description" htmlFor="smart_collection_description">
            <textarea
              id="smart_collection_description"
              name="description"
              rows={2}
              placeholder="Optional description..."
              value={editor.description}
              onChange={(e) => editor.setDescription(e.target.value)}
              className={inputClass}
            />
          </FormField>
        </div>

        {/* 2. Library Filter Fields */}
        <div className="border-t border-[#222222] pt-4">
          {catalogEmpty && (
            <p className="mb-3 text-xs font-mono text-neutral-500">
              Your library catalog is empty — scan your library before building a smart collection.
            </p>
          )}
          <LibraryFilterFields
            filters={editor.filters}
            onChange={editor.setFilters}
            facets={facets}
            tags={tags}
          />
        </div>

        {/* 3. Sort & Limit */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 border-t border-[#222222] pt-4">
          <FormField label="Sort Order" htmlFor="smart_collection_sort">
            <select
              id="smart_collection_sort"
              name="sort"
              value={editor.sort}
              onChange={(e) => editor.setSort(e.target.value as SmartSortOption)}
              className={inputClass}
            >
              {(Object.keys(SMART_SORT_LABELS) as SmartSortOption[]).map((val) => (
                <option key={val} value={val}>
                  {SMART_SORT_LABELS[val]}
                </option>
              ))}
            </select>
          </FormField>

          <FormField
            label="Limit (1–1000)"
            htmlFor="smart_collection_limit"
            hint="Maximum number of tracks to select"
          >
            <input
              id="smart_collection_limit"
              name="limit"
              type="number"
              min={1}
              max={1000}
              value={editor.limit}
              onChange={(e) => {
                const val = parseInt(e.target.value, 10);
                editor.setLimit(Number.isNaN(val) ? 1 : Math.max(1, Math.min(1000, val)));
              }}
              className={inputClass}
            />
          </FormField>
        </div>

        {/* 4. Update mode segmented control */}
        <div className="border-t border-[#222222] pt-4 space-y-1.5">
          <span
            id="smart_collection_update_mode_label"
            className="block text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)]"
          >
            Update mode
          </span>
          <div
            role="group"
            aria-labelledby="smart_collection_update_mode_label"
            className="tape-transport-bay p-1 grid grid-cols-2 gap-1 rounded-[4px]"
          >
            <button
              type="button"
              aria-pressed={!editor.autoUpdate}
              onClick={() => editor.setAutoUpdate(false)}
              className={`tape-deck-btn px-3 py-2 text-xs font-mono uppercase tracking-wider rounded-[3px] transition-colors ${
                !editor.autoUpdate
                  ? 'engaged text-white border-[var(--accent-amber)]'
                  : 'text-neutral-400 hover:text-white'
              }`}
            >
              One-time
            </button>
            <button
              type="button"
              aria-pressed={editor.autoUpdate}
              onClick={() => editor.setAutoUpdate(true)}
              className={`tape-deck-btn px-3 py-2 text-xs font-mono uppercase tracking-wider rounded-[3px] transition-colors ${
                editor.autoUpdate
                  ? 'engaged text-white border-[var(--accent-amber)]'
                  : 'text-neutral-400 hover:text-white'
              }`}
            >
              Auto-update
            </button>
          </div>
          <p className="text-[11px] font-mono text-neutral-400">
            {editor.autoUpdate
              ? 'Rebuilt on every sync as your library changes.'
              : 'Pushed now; only changes when you press Sync now.'}
          </p>
        </div>

        {/* 5. Targets */}
        {canTargetUsers && users.length > 0 && (
          <div className="border-t border-[#222222] pt-4 space-y-2">
            <span
              id="smart_collection_targets_label"
              className="block text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)]"
            >
              Sync Targets {serverLabel ? `(${serverLabel})` : ''}
            </span>
            <div
              role="group"
              aria-labelledby="smart_collection_targets_label"
              className="flex flex-wrap gap-3"
            >
              {users.map((u) => {
                const uIdStr = String(u.id);
                const isTarget = editor.targets.includes(uIdStr);
                return (
                  <label
                    key={u.id}
                    htmlFor={`smart_target_${u.id}`}
                    className="inline-flex items-center gap-2 cursor-pointer text-xs font-mono text-neutral-300 hover:text-white select-none"
                  >
                    <input
                      id={`smart_target_${u.id}`}
                      name="targets"
                      type="checkbox"
                      checked={isTarget}
                      onChange={() => {
                        if (isTarget) {
                          editor.setTargets(editor.targets.filter((id) => id !== uIdStr));
                        } else {
                          editor.setTargets([...editor.targets, uIdStr]);
                        }
                      }}
                      className="accent-[var(--accent-amber)] rounded-[2px]"
                    />
                    <span>{u.username}</span>
                  </label>
                );
              })}
            </div>
          </div>
        )}

        {/* 6. Live preview panel */}
        <div className="border-t border-[#222222] pt-4 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] font-semibold">
              Live Preview
            </span>
            {editor.preview.loading ? (
              <span className="text-xs font-mono text-neutral-400 flex items-center gap-1.5">
                <Loader2 className="h-3 w-3 animate-spin" />
                Scanning...
              </span>
            ) : (
              <span className="text-xs font-mono text-[var(--accent-amber)] font-semibold">
                {editor.preview.count > 0 ? (
                  previewCap ? (
                    `${matchDisplayCount} tracks match (of ${editor.preview.count})`
                  ) : (
                    `${editor.preview.count} track${editor.preview.count === 1 ? '' : 's'} match`
                  )
                ) : (
                  '0 tracks match'
                )}
              </span>
            )}
          </div>

          {editor.preview.count === 0 && !editor.preview.loading ? (
            <div className="p-3 bg-[#161616] border border-[#222222] rounded-[3px] text-xs font-mono text-neutral-400 text-center">
              Nothing in your library matches yet.
            </div>
          ) : (
            <div className="max-h-48 sm:max-h-56 overflow-y-auto bg-[#101010] border border-[#222222] rounded-[3px] divide-y divide-[#1c1c1c]">
              {editor.preview.tracks.slice(0, 25).map((track, idx) => (
                <div
                  key={`${track.artist}-${track.title}-${idx}`}
                  className="px-2.5 py-1.5 text-xs font-mono text-neutral-300 flex items-center justify-between gap-2"
                >
                  <span className="truncate">
                    <span className="text-neutral-400">{track.artist}</span>
                    <span className="text-neutral-600 mx-1.5">—</span>
                    <span className="text-white">{track.title}</span>
                  </span>
                  {track.year != null && (
                    <span className="text-[11px] text-neutral-500 shrink-0 font-mono">
                      {track.year}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </form>
    </ObsidianModal>
  );
};
