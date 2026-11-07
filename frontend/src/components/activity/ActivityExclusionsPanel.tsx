import React, { useCallback, useMemo, useState } from 'react';
import { Trash2 } from 'lucide-react';
import type { ExclusionItem } from '@/services/exclusionsService';
import { useExclusions, getExclusionKey } from '@/hooks/useExclusions';
import { useDebouncedValue } from '@/hooks/useDebouncedValue';
import { ConfirmDangerButton, SearchBar, TapeDeckButton, type OverflowMenuItem } from '@/components/ui';
import { FlatList, ListPanel, formatDateTime, orDash, type FlatListColumn } from '@/components/lists';
import type { ActivityPanelProps } from './ActivityQueuePanel';

const SCOPES: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'artist', label: 'Artists' },
  { value: 'album', label: 'Albums' },
  { value: 'track', label: 'Tracks' },
];

function formatExclusionName(item: ExclusionItem): string {
  if (item.scope === 'artist' || !item.title) {
    return item.artist_name;
  }
  return `${item.title} by ${item.artist_name}`;
}

export const ActivityExclusionsPanel: React.FC<ActivityPanelProps> = ({ onToast }) => {
  const [scope, setScope] = useState<string>('all');
  const [search, setSearch] = useState<string>('');
  const debouncedSearch = useDebouncedValue(search, 250);
  const [busy, setBusy] = useState<boolean>(false);

  const { list, removeExclusion } = useExclusions({
    scope,
    search: debouncedSearch,
    onToast,
  });

  const handleRemove = useCallback(
    async (id: string) => {
      setBusy(true);
      try {
        await removeExclusion(id);
      } finally {
        setBusy(false);
      }
    },
    [removeExclusion]
  );

  const columns = useMemo<FlatListColumn<ExclusionItem>[]>(
    () => [
      {
        key: 'scope',
        label: 'Type',
        width: '90px',
        mobile: 'meta',
        render: (r) => (
          <span className="capitalize font-mono text-xs px-1.5 py-0.5 rounded-[2px] border border-[#2a2a2a] bg-[#161616] text-neutral-300">
            {r.scope}
          </span>
        ),
      },
      {
        key: 'name',
        label: 'Name',
        width: 'minmax(0, 1.6fr)',
        mobile: 'title',
        truncate: true,
        render: (r) => (
          <span className="text-white font-medium truncate" title={formatExclusionName(r)}>
            {formatExclusionName(r)}
          </span>
        ),
      },
      {
        key: 'reason',
        label: 'Reason',
        width: 'minmax(0, 1.2fr)',
        mobile: 'sub',
        truncate: true,
        render: (r) => orDash(r.reason),
      },
      {
        key: 'added',
        label: 'Added',
        width: '180px',
        mobile: 'meta',
        render: (r) => {
          const dateStr = formatDateTime(r.created_at);
          return r.created_by ? `${dateStr} (${r.created_by})` : dateStr;
        },
      },
      {
        key: 'actions',
        label: '',
        width: '80px',
        mobile: 'end',
        render: (r) => (
          <ConfirmDangerButton
            icon={<Trash2 className="h-3.5 w-3.5" />}
            idleLabel="Remove"
            collapseLabel
            ariaLabel={`Remove exclusion for ${formatExclusionName(r)}`}
            confirmLabel="Remove"
            disabled={busy}
            onConfirm={() => void handleRemove(r.id)}
          />
        ),
      },
    ],
    [busy, handleRemove]
  );

  const rowMenu = useCallback(
    (r: ExclusionItem): OverflowMenuItem[] => [
      {
        key: 'remove',
        label: 'Remove exclusion…',
        icon: <Trash2 className="h-3.5 w-3.5" />,
        disabled: busy,
        onSelect: () => void handleRemove(r.id),
      },
    ],
    [busy, handleRemove]
  );

  const rowMenuLabel = useCallback(
    (r: ExclusionItem) => `Actions for ${formatExclusionName(r)}`,
    []
  );

  const rowDetails = useCallback((r: ExclusionItem): React.ReactNode | null => {
    const details: { label: string; value: string }[] = [];
    if (r.scope) details.push({ label: 'Scope', value: r.scope });
    if (r.artist_name) details.push({ label: 'Artist', value: r.artist_name });
    if (r.title) details.push({ label: 'Title', value: r.title });
    if (r.mbid) details.push({ label: 'MusicBrainz ID', value: r.mbid });
    if (r.reason) details.push({ label: 'Reason', value: r.reason });
    if (r.created_at) details.push({ label: 'Added', value: formatDateTime(r.created_at) });
    if (r.created_by) details.push({ label: 'Added by', value: r.created_by });

    if (details.length === 0) return null;
    return (
      <div className="flex flex-col gap-0.5 text-xs font-mono">
        {details.map((d) => (
          <div key={d.label}>
            <span className="text-neutral-500">{d.label}:</span>{' '}
            <span className="text-neutral-300">{d.value}</span>
          </div>
        ))}
      </div>
    );
  }, []);

  const toolbar = (
    <div className="flex flex-wrap items-center gap-2">
      <div role="group" aria-label="Filter exclusions by scope" className="tape-transport-bay flex items-center gap-1">
        {SCOPES.map((s) => (
          <TapeDeckButton
            key={s.value}
            size="sm"
            active={scope === s.value}
            onClick={() => setScope(s.value)}
          >
            {s.label}
          </TapeDeckButton>
        ))}
      </div>
      <div className="w-36 sm:w-56">
        <SearchBar
          value={search}
          onChange={setSearch}
          placeholder="Search exclusions…"
          ariaLabel="Search exclusions"
        />
      </div>
    </div>
  );

  return (
    <ListPanel
      title="Exclusions"
      description="Artists, albums and songs excluded from requests, lists and automatic searching."
      mode={list.mode}
      total={list.total}
      toolbar={toolbar}
    >
      <FlatList
        ariaLabel="Exclusions"
        columns={columns}
        list={list}
        getKey={getExclusionKey}
        sortKey="date"
        sortDir="desc"
        onSortChange={() => undefined}
        layout="card"
        rowMenu={rowMenu}
        rowMenuLabel={rowMenuLabel}
        rowDetails={rowDetails}
        emptyMessage='Nothing is excluded. Use "Exclude" on an artist, album or song to stop TrackSeerr from ever getting it.'
      />
    </ListPanel>
  );
};
