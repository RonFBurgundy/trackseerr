import React, { useState } from 'react';
import { Ban } from 'lucide-react';
import type { ItemHistoryEntity } from '@/types/itemHistory';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
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
}) => {
  const [reloadKey, setReloadKey] = useState<number>(0);
  const [excludeOpen, setExcludeOpen] = useState<boolean>(false);
  const rejectEntity = entity === 'album' || entity === 'track' ? entity : null;
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
            <TapeDeckButton
              size="sm"
              icon={<Ban className="h-3.5 w-3.5" />}
              onClick={() => setExcludeOpen(true)}
            >
              Exclude track…
            </TapeDeckButton>
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
      <ExcludeConfirm
        isOpen={excludeOpen}
        scope="track"
        name={title}
        payload={{ library_track_id: entityId }}
        onClose={() => setExcludeOpen(false)}
        onSuccess={() => setReloadKey((k) => k + 1)}
        onToast={onToast}
      />
    )}
    </>
  );
};
