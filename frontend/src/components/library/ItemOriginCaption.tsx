import React, { useCallback, useState } from 'react';
import type { ItemHistoryEntity } from '@/types/itemHistory';
import { useItemOrigin } from '@/hooks/useItemHistory';
import { ItemHistoryModal } from './ItemHistoryModal';
import { originCaption } from './itemHistoryFormat';

export interface ItemOriginCaptionProps {
  entity: ItemHistoryEntity;
  /** Library id of the item; the caption renders nothing without one. */
  entityId: string | number | null | undefined;
  /** Item name for the history modal subtitle. */
  title: string;
  className?: string;
}

/** One muted line ("Requested by ron · Oct 3") that opens the item's history. Text link, no button chrome. */
export const ItemOriginCaption: React.FC<ItemOriginCaptionProps> = ({ entity, entityId, title, className = '' }) => {
  const id = entityId === null || entityId === undefined || entityId === '' ? null : String(entityId);
  const { origin } = useItemOrigin(entity, id);
  const [open, setOpen] = useState<boolean>(false);
  const close = useCallback((): void => setOpen(false), []);
  if (id === null) return null;
  const text = origin ? originCaption(origin) : null;
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title="View history"
        className={`block max-w-full truncate text-left font-mono text-[11px] text-neutral-500 hover:text-neutral-300 hover:underline ${className}`}
      >
        {text ?? 'View history'}
      </button>
      {open && <ItemHistoryModal isOpen onClose={close} entity={entity} entityId={id} title={title} />}
    </>
  );
};
