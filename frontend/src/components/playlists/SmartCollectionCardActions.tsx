import React, { useState } from 'react';
import { RefreshCw, Pencil, Loader2 } from 'lucide-react';
import type { Playlist } from '@/types/models';
import { TapeDeckButton } from '@/components/ui';
import { syncSmartCollection } from '@/services/smartCollectionService';
import { errorMessage } from '@/services/apiClient';

export interface SmartCollectionCardActionsProps {
  playlist: Playlist;
  onEdit: (collectionId: string) => void;
  onPlaylistsChanged?: () => Promise<void>;
  onToast: (message: string, tone?: 'ok' | 'error') => void;
}

export const SmartCollectionCardActions: React.FC<SmartCollectionCardActionsProps> = ({
  playlist,
  onEdit,
  onPlaylistsChanged,
  onToast,
}) => {
  const [isSyncing, setIsSyncing] = useState<boolean>(false);

  const handleSync = async () => {
    setIsSyncing(true);
    try {
      const res = await syncSmartCollection(playlist.id);
      onToast(`Synced "${res.name}": ${res.matched_count} of ${res.track_count} tracks matched`);
      await onPlaylistsChanged?.();
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to sync smart collection'), 'error');
    } finally {
      setIsSyncing(false);
    }
  };

  return (
    <div className="flex items-center gap-1.5">
      <TapeDeckButton
        size="sm"
        onClick={() => void handleSync()}
        disabled={isSyncing}
        aria-label={`Sync smart collection ${playlist.name}`}
        title={`Sync ${playlist.name}`}
        icon={
          isSyncing ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )
        }
      >
        {isSyncing ? 'Syncing...' : 'Sync now'}
      </TapeDeckButton>
      <TapeDeckButton
        size="sm"
        onClick={() => onEdit(playlist.id)}
        aria-label={`Edit smart collection ${playlist.name}`}
        title={`Edit ${playlist.name}`}
        icon={<Pencil className="h-3.5 w-3.5" />}
      >
        Edit
      </TapeDeckButton>
    </div>
  );
};
