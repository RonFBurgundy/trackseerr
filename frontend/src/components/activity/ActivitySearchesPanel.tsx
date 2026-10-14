import React, { useCallback, useMemo, useState } from 'react';
import type { ActivitySearchRecord } from '@/types/activity';
import { getActivitySearches } from '@/services/activityService';
import { useVirtualPagedList, type FetchPage } from '@/hooks/useVirtualPagedList';
import { TapeDeckButton } from '@/components/ui';
import { FlatList, ListPanel, formatDateTime, orDash, type FlatListColumn } from '@/components/lists';
import type { ActivityPanelProps } from './ActivityQueuePanel';

type SourceFilter = '' | 'slskd' | 'torznab' | 'newznab';

const FILTERS: ReadonlyArray<{ id: SourceFilter; label: string }> = [
  { id: '', label: 'All' },
  { id: 'slskd', label: 'Soulseek' },
  { id: 'torznab', label: 'Torznab' },
  { id: 'newznab', label: 'Newznab' },
];

const TRIGGER_LABELS: Readonly<Record<string, string>> = {
  request: 'Request',
  request_approved: 'Approved request',
  wanted: 'Wanted sweep',
  rss: 'RSS',
  playlist: 'Playlist',
  mix: 'Mix',
  import_list: 'Import list',
  manual: 'Manual search',
  issue: 'Issue',
  upgrade: 'Upgrade',
  retry: 'Retry',
  user: 'User',
};

const fetchSearches: FetchPage<ActivitySearchRecord> = async (req) => {
  const source = req.filters.source_type;
  const res = await getActivitySearches(
    { page: req.page, pageSize: req.pageSize, sourceType: typeof source === 'string' && source ? source : undefined },
    req.signal
  );
  return { items: res.records, total: res.total };
};

const getKey = (r: ActivitySearchRecord): string | number => r.id;

function queryText(r: ActivitySearchRecord): string {
  const who = r.query_artist ?? '';
  const what = r.query_title || r.query_album || '';
  return [who, what].filter(Boolean).join(' \u2014 ');
}

function triggerText(r: ActivitySearchRecord): string {
  if (!r.trigger_kind) return '\u2014';
  const base = TRIGGER_LABELS[r.trigger_kind] ?? r.trigger_kind;
  return r.trigger_label ? `${base}: ${r.trigger_label}` : base;
}

/** Read-only log of every indexer and Soulseek search with what triggered it. */
export const ActivitySearchesPanel: React.FC<ActivityPanelProps> = () => {
  const [source, setSource] = useState<SourceFilter>('');
  const filters = useMemo(() => ({ source_type: source }), [source]);
  const list = useVirtualPagedList<ActivitySearchRecord>(fetchSearches, {
    sortKey: 'created_at',
    sortDir: 'desc',
    filters,
    getKey,
  });

  const columns = useMemo<FlatListColumn<ActivitySearchRecord>[]>(
    () => [
      { key: 'created_at', label: 'When', width: '150px', mobile: 'meta', render: (r) => formatDateTime(r.created_at) },
      {
        key: 'source',
        label: 'Source',
        width: 'minmax(100px,0.8fr)',
        truncate: true,
        mobile: 'sub',
        render: (r) => (
          <span className="inline-flex items-center gap-1.5 min-w-0">
            <span className="truncate">{orDash(r.source_name)}</span>
            {r.source_type && (
              <span className="shrink-0 px-1 rounded-[2px] border border-[#2a2a2a] text-[9px] font-mono uppercase text-neutral-400">
                {r.source_type}
              </span>
            )}
          </span>
        ),
      },
      {
        key: 'query',
        label: 'Query',
        width: 'minmax(160px,1.6fr)',
        truncate: true,
        mobile: 'title',
        render: (r) => (
          <span className="inline-flex items-center gap-1.5 min-w-0">
            <span className="truncate">{orDash(queryText(r))}</span>
            {r.rung != null && r.rung > 1 && (
              <span className="shrink-0 px-1 rounded-[2px] border border-[#e5a00d]/50 text-[9px] font-mono uppercase text-[#e5a00d]">
                retry {r.rung}
              </span>
            )}
          </span>
        ),
      },
      {
        key: 'results',
        label: 'Results',
        width: 'minmax(70px,0.6fr)',
        mobile: 'meta',
        render: (r) =>
          r.error ? (
            <span className="text-[var(--status-error)] break-words" title={r.error}>
              {r.error}
            </span>
          ) : (
            String(r.result_count ?? '\u2014')
          ),
      },
      {
        key: 'took',
        label: 'Took',
        width: '64px',
        mobile: 'meta',
        render: (r) => (r.duration_ms == null ? '\u2014' : `${(r.duration_ms / 1000).toFixed(1)}s`),
      },
      { key: 'trigger', label: 'Trigger', width: 'minmax(120px,1fr)', truncate: true, mobile: 'end', render: (r) => triggerText(r) },
    ],
    []
  );

  const rowDetails = useCallback((r: ActivitySearchRecord): React.ReactNode | null => {
    const details: { label: string; value: string }[] = [];
    if (r.query_artist && r.query_album) {
      details.push({ label: 'Artist', value: r.query_artist });
      details.push({ label: 'Album', value: r.query_album });
    }
    if (r.duration_ms != null) {
      details.push({ label: 'Took', value: `${(r.duration_ms / 1000).toFixed(1)}s` });
    }
    if (r.created_at) {
      details.push({ label: 'When', value: formatDateTime(r.created_at) });
    }

    if (details.length === 0) return null;
    return (
      <div className="flex flex-col gap-0.5">
        {details.map((d) => (
          <div key={d.label}>
            <span className="text-neutral-500">{d.label}:</span> {d.value}
          </div>
        ))}
      </div>
    );
  }, []);

  const toolbar = (
    <>
      {FILTERS.map((f) => (
        <TapeDeckButton key={f.id || 'all'} size="sm" active={source === f.id} onClick={() => setSource(f.id)}>
          {f.label}
        </TapeDeckButton>
      ))}
    </>
  );

  return (
    <ListPanel
      title="Searches"
      description="Every indexer and Soulseek search, with what triggered it."
      mode={list.mode}
      total={list.total}
      toolbar={toolbar}
    >
      <FlatList
        ariaLabel="Search log"
        columns={columns}
        list={list}
        getKey={getKey}
        sortKey="created_at"
        sortDir="desc"
        onSortChange={() => undefined}
        layout="card"
        rowDetails={rowDetails}
        emptyMessage="No searches logged yet."
      />
    </ListPanel>
  );
};
