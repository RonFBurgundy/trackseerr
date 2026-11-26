import React, { useId, useState } from 'react';
import { Check, Pencil, Trash2 } from 'lucide-react';
import { ConfirmDangerButton, MachinedCard, TapeDeckButton } from '@/components/ui';
import { inputClass, labelClass } from '@/components/ui/FormField';
import type { LibraryHealthMapping } from '@/types/libraryHealth';

export interface ReviewMappingCardProps {
  mapping: LibraryHealthMapping | null;
  serverId?: string;
  serverName?: string;
  busy: boolean;
  onSave: (serverPrefix: string, localPrefix: string, serverId?: string) => Promise<void>;
  onRemove: (serverId?: string) => Promise<void>;
}

/** Shows how the media server's file paths map onto Trackseerr's, with confirm / edit / remove. */
export const ReviewMappingCard: React.FC<ReviewMappingCardProps> = React.memo(({ mapping, serverId, serverName, busy, onSave, onRemove }) => {
  const serverInputId = useId();
  const localInputId = useId();
  const [editing, setEditing] = useState<boolean>(false);
  const [serverPrefix, setServerPrefix] = useState<string>('');
  const [localPrefix, setLocalPrefix] = useState<string>('');

  const startEdit = (): void => {
    setServerPrefix(mapping?.server_prefix ?? '');
    setLocalPrefix(mapping?.local_prefix ?? '');
    setEditing(true);
  };

  const save = async (): Promise<void> => {
    await onSave(serverPrefix, localPrefix, serverId);
    setEditing(false);
  };

  const canSave = serverPrefix.trim() !== '' && localPrefix.trim() !== '' && !busy;

  return (
    <MachinedCard className="p-3 space-y-2" aria-label={serverName ? `Path mapping (${serverName})` : 'Path mapping'}>
      {serverName && (
        <p className="text-[11px] font-bold uppercase tracking-widest font-mono text-[var(--text-secondary)]">{serverName}</p>
      )}
      {editing ? (
        <form
          className="space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (canSave) void save();
          }}
        >
          <div className="grid gap-2 sm:grid-cols-2">
            <div>
              <label htmlFor={serverInputId} className={labelClass}>
                Path the media server sees
              </label>
              <input
                id={serverInputId}
                name="library-health-server-prefix"
                type="text"
                value={serverPrefix}
                onChange={(e) => setServerPrefix(e.target.value)}
                placeholder="/music"
                autoComplete="off"
                className={`${inputClass} font-mono`}
              />
            </div>
            <div>
              <label htmlFor={localInputId} className={labelClass}>
                Same folder in Trackseerr
              </label>
              <input
                id={localInputId}
                name="library-health-local-prefix"
                type="text"
                value={localPrefix}
                onChange={(e) => setLocalPrefix(e.target.value)}
                placeholder="/data/music"
                autoComplete="off"
                className={`${inputClass} font-mono`}
              />
            </div>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <TapeDeckButton type="submit" size="sm" variant="amber" disabled={!canSave} icon={<Check className="h-3.5 w-3.5" />}>
              Save mapping
            </TapeDeckButton>
            <TapeDeckButton size="sm" disabled={busy} onClick={() => setEditing(false)}>
              Cancel
            </TapeDeckButton>
          </div>
        </form>
      ) : (
        <div className="flex flex-col sm:flex-row sm:items-center gap-2">
          <p className="flex-1 min-w-0 text-xs font-mono text-[var(--text-secondary)]">
            {mapping === null ? (
              'No path mapping. Add one if your media server sees your music folder under a different path.'
            ) : (
              <>
                {mapping.auto ? 'We guessed your server sees ' : 'Your server sees '}
                <span className="text-[var(--text-primary)] break-all">{mapping.local_prefix}</span>
                {' as '}
                <span className="text-[var(--text-primary)] break-all">{mapping.server_prefix}</span>
                {mapping.auto ? '.' : ' (confirmed).'}
              </>
            )}
          </p>
          <div className="flex flex-wrap gap-1.5">
            {mapping?.auto && (
              <TapeDeckButton
                size="sm"
                variant="amber"
                disabled={busy}
                onClick={() => void onSave(mapping.server_prefix, mapping.local_prefix, serverId)}
                icon={<Check className="h-3.5 w-3.5" />}
              >
                Confirm
              </TapeDeckButton>
            )}
            <TapeDeckButton size="sm" disabled={busy} onClick={startEdit} icon={<Pencil className="h-3.5 w-3.5" />}>
              {mapping === null ? 'Add mapping' : 'Edit'}
            </TapeDeckButton>
            {mapping !== null && (
              <ConfirmDangerButton
                icon={<Trash2 className="h-3.5 w-3.5" />}
                idleLabel="Remove mapping"
                ariaLabel="Remove path mapping"
                confirmLabel="Remove"
                disabled={busy}
                onConfirm={() => void onRemove(serverId)}
              />
            )}
          </div>
        </div>
      )}
    </MachinedCard>
  );
});
ReviewMappingCard.displayName = 'ReviewMappingCard';
