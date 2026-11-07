import React, { useState } from 'react';
import { Ban } from 'lucide-react';
import type { ItemHistoryEntity } from '@/types/itemHistory';
import { ConfirmDialog, ObsidianModal, TapeDeckButton } from '@/components/ui';
import { deleteExclusion } from '@/services/exclusionsService';
import { errorMessage } from '@/services/apiClient';
import { ItemHistoryTimeline } from './ItemHistoryTimeline';
import { RejectCurrentButton } from './RejectCurrentButton';
import { ExcludeConfirm } from './ExcludeConfirm';

export interface ItemHistoryModalProps {
  isOpen: boolean;
  onClose: () => void;
  entity: ItemHistoryEntity;
  entityId: string;
  /** Shown as the modal subtitle. */
  title: string;
  /** Shows "Reject current release" in the footer (admin, native mode; album/track only). Needs `onToast`. */
  canReject?: boolean;
  onToast?: (msg: string, tone?: 'ok' | 'error') => void;
  excluded?: boolean;
  exclusionId?: string | null;
  exclusionReason?: string | null;
  artistExcluded?: boolean;
  onExclusionChanged?: () => void;
}

/** An item's audit trail as a newest-first timeline with keyset "load older". Fetches only while open. */
export const ItemHistoryModal: React.FC<ItemHistoryModalProps> = ({
  isOpen,
  onClose,
  entity,
  entityId,
  title,
  canReject = false,
  onToast,
  excluded = false,
  exclusionId,
  artistExcluded = false,
  onExclusionChanged,
}) => {
  const [reloadKey, setReloadKey] = useState<number>(0);
  const [excludeOpen, setExcludeOpen] = useState<boolean>(false);
  const [removeConfirmOpen, setRemoveConfirmOpen] = useState<boolean>(false);
  const [removeBusy, setRemoveBusy] = useState<boolean>(false);
  const rejectEntity = entity === 'album' || entity === 'track' ? entity : null;

  const handleRemoveExclusion = async (): Promise<void> => {
    if (!exclusionId) return;
    setRemoveBusy(true);
    try {
      await deleteExclusion(exclusionId);
      onToast?.('Exclusion removed', 'ok');
      setRemoveConfirmOpen(false);
      onExclusionChanged?.();
      onClose();
    } catch (err: unknown) {
      onToast?.(errorMessage(err, 'Failed to remove exclusion'), 'error');
    } finally {
      setRemoveBusy(false);
    }
  };

  return (
    <>
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="History"
      subtitle={title}
      footer={
        <>
          {canReject && rejectEntity && onToast && (
            <RejectCurrentButton
              entity={rejectEntity}
              entityId={entityId}
              onToast={onToast}
              onDone={() => setReloadKey((k) => k + 1)}
            />
          )}
          {canReject && entity === 'track' && onToast && (
            excluded ? (
              artistExcluded ? (
                <TapeDeckButton
                  size="sm"
                  disabled
                  icon={<Ban className="h-3.5 w-3.5 text-neutral-500" />}
                  title="Excluded with the artist (undo on artist page)"
                >
                  Excluded with the artist
                </TapeDeckButton>
              ) : (
                <TapeDeckButton
                  size="sm"
                  variant="danger"
                  icon={<Ban className="h-3.5 w-3.5 text-red-400" />}
                  onClick={() => setRemoveConfirmOpen(true)}
                >
                  Remove exclusion
                </TapeDeckButton>
              )
            ) : (
              <TapeDeckButton
                size="sm"
                icon={<Ban className="h-3.5 w-3.5" />}
                onClick={() => setExcludeOpen(true)}
              >
                Exclude track…
              </TapeDeckButton>
            )
          )}
          <TapeDeckButton size="sm" onClick={onClose}>
            Close
          </TapeDeckButton>
        </>
      }
    >
      {isOpen && <ItemHistoryTimeline key={reloadKey} entity={entity} entityId={entityId} />}
    </ObsidianModal>
    {canReject && entity === 'track' && onToast && (
      <>
        <ExcludeConfirm
          isOpen={excludeOpen}
          scope="track"
          name={title}
          payload={{ library_track_id: entityId }}
          onClose={() => setExcludeOpen(false)}
          onSuccess={() => {
            setReloadKey((k) => k + 1);
            onExclusionChanged?.();
            onClose();
          }}
          onToast={onToast}
        />
        <ConfirmDialog
          isOpen={removeConfirmOpen}
          title="Remove exclusion?"
          confirmLabel="Remove exclusion"
          onConfirm={() => void handleRemoveExclusion()}
          onCancel={() => setRemoveConfirmOpen(false)}
          busy={removeBusy}
        >
          <p className="text-xs sm:text-sm font-mono text-neutral-300">
            Allow &ldquo;{title}&rdquo; again? It stays unmonitored until you monitor it.
          </p>
        </ConfirmDialog>
      </>
    )}
    </>
  );
};
