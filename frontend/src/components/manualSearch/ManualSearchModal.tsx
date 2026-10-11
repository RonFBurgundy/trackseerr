import React, { useEffect, useId, useState } from 'react';
import { AlertTriangle, Check, ChevronDown, ChevronRight, Download, Loader2, RefreshCw, X } from 'lucide-react';
import { useManualSearch } from '@/hooks/useManualSearch';
import type { ManualSearchFilter, ManualSearchSort, ManualSearchTarget, SearchRelease } from '@/types/manualSearch';
import { CassetteLoader, ConfirmDangerButton, ObsidianModal, TabStrip, TapeDeckButton } from '@/components/ui';
import { ItemHistoryTimeline } from '@/components/library/ItemHistoryTimeline';
import { RejectCurrentButton } from '@/components/library/RejectCurrentButton';
import { historyRelativeTime } from '@/components/library/itemHistoryFormat';
import { formatBytes } from '@/components/lists';

export interface ManualSearchModalProps {
  /** Null closes the modal. Pass a stable object (state); a new object restarts the search. */
  target: ManualSearchTarget | null;
  onClose: () => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
  /** Fires after a release was sent to a download client. */
  onGrabbed?: () => void;
}

const FILTERS: ReadonlyArray<{ value: ManualSearchFilter; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'accepted', label: 'Accepted' },
  { value: 'rejected', label: 'Rejected' },
];

const SORTS: ReadonlyArray<{ value: ManualSearchSort; label: string }> = [
  { value: 'default', label: 'Best match' },
  { value: 'score', label: 'Score' },
  { value: 'size', label: 'Size' },
  { value: 'seeders', label: 'Seeders' },
  { value: 'age', label: 'Newest' },
];

/** Compact age such as `3d`, `5mo`, `2y`; `-` when the indexer gave no (or an unparsable) publish date. */
function formatAge(value: string | null | undefined): string {
  if (!value) return '-';
  const t = Date.parse(value);
  if (Number.isNaN(t)) return '-';
  const days = Math.max(0, Math.floor((Date.now() - t) / 86_400_000));
  if (days < 1) return '<1d';
  if (days < 60) return `${days}d`;
  if (days < 730) return `${Math.floor(days / 30)}mo`;
  return `${Math.floor(days / 365)}y`;
}

/** Local date-time for a server timestamp; the raw text when it does not parse. */
function formatWhen(value: string): string {
  const t = Date.parse(value);
  return Number.isNaN(t) ? value : new Date(t).toLocaleString();
}

/** Blocklist match carried on a release: reason/date as one line for the badge tooltip, or null when not blocklisted. */
function blocklistNote(release: SearchRelease): string | null {
  const b = release.blocklist;
  if (!b) return null;
  const reason = typeof b.reason === 'string' && b.reason !== '' ? b.reason : 'Blocklisted';
  const indexer = typeof b.indexer === 'string' && b.indexer !== '' ? ` on ${b.indexer}` : '';
  const when = typeof b.created_at === 'string' && b.created_at !== '' ? ` (${formatWhen(b.created_at)})` : '';
  return `${reason}${indexer}${when}`;
}

const BADGE =
  'px-1 py-px rounded-[2px] text-[10px] uppercase border flex-shrink-0 font-mono';

const ReleaseBadges: React.FC<{ release: SearchRelease }> = ({ release }) => {
  const note = blocklistNote(release);
  return (
    <>
      {note && (
        <span className={`${BADGE} border-[#ef4444]/40 text-[#ef4444]`} title={note}>
          Blocklisted
        </span>
      )}
      {release.in_queue && (
        <span className={`${BADGE} border-[#e5a00d]/40 text-[#e5a00d]`} title="This release is already in the download queue">
          Downloading
        </span>
      )}
      {release.pending_until && (
        <span
          className={`${BADGE} border-[#2a2a2a] text-neutral-300`}
          title={`Held by the delay profile until ${formatWhen(release.pending_until)}`}
        >
          Held until {formatWhen(release.pending_until)}
        </span>
      )}
    </>
  );
};

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}

const ROW_GRID =
  'sm:grid sm:grid-cols-[20px_minmax(0,1fr)_140px_90px_64px_52px_44px_72px_128px] sm:items-center sm:gap-3';

interface ReleaseRowProps {
  release: SearchRelease;
  expanded: boolean;
  busy: boolean;
  grabbed: boolean;
  onToggle: (id: string) => void;
  onGrab: (release: SearchRelease) => void;
}

const StatusIcon: React.FC<{ release: SearchRelease }> = ({ release }) => {
  if (!release.is_acceptable) {
    return <X className="h-4 w-4 text-[#ef4444]" aria-label="Rejected" />;
  }
  if (!release.meets_cutoff) {
    return <Check className="h-4 w-4 text-[#e5a00d]" aria-label="Accepted, below cutoff" />;
  }
  return <Check className="h-4 w-4 text-[#22c55e]" aria-label="Accepted" />;
};

const ReleaseRow = React.memo<ReleaseRowProps>(({ release: r, expanded, busy, grabbed, onToggle, onGrab }) => {
  const isTorrent = r.protocol === 'torrent';
  const detailId = `release-detail-${r.id}`;
  const breakdown = r.breakdown ? Object.entries(r.breakdown) : [];
  const grabButton = grabbed ? (
    <span className="text-xs font-mono uppercase tracking-wider text-[#22c55e]">Grabbed</span>
  ) : r.is_acceptable ? (
    <TapeDeckButton
      size="sm"
      disabled={busy}
      onClick={() => onGrab(r)}
      aria-label={`Download ${r.title}`}
      icon={busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
    >
      Download
    </TapeDeckButton>
  ) : (
    <ConfirmDangerButton
      icon={busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
      idleLabel="Download"
      ariaLabel={`Download rejected release ${r.title}`}
      confirmLabel="Grab anyway"
      disabled={busy}
      onConfirm={() => onGrab(r)}
    />
  );

  return (
    <li className={`border-b border-[#181818] ${r.is_acceptable ? '' : 'opacity-60'}`}>
      <div className={`px-2.5 py-2 flex flex-col gap-1 ${ROW_GRID} hover:bg-[#141414] transition-colors`}>
        <div className="flex items-center gap-2 min-w-0 sm:contents">
          <span className="flex-shrink-0 flex items-center justify-center">
            <StatusIcon release={r} />
          </span>
          <button
            type="button"
            onClick={() => onToggle(r.id)}
            aria-expanded={expanded}
            aria-controls={detailId}
            className="min-w-0 flex-1 flex items-center gap-1 text-left text-sm text-neutral-200 hover:text-white"
            title={r.title}
          >
            {expanded ? (
              <ChevronDown className="h-3.5 w-3.5 flex-shrink-0 text-neutral-500" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 flex-shrink-0 text-neutral-500" />
            )}
            <span className="truncate">{r.title}</span>
          </button>
          <span className="sm:hidden font-mono text-xs text-neutral-300 flex-shrink-0" title="Score">
            {r.score}
          </span>
        </div>
        <div className="flex items-center gap-1.5 min-w-0 text-xs font-mono text-neutral-400 truncate">
          <span className="truncate">{r.indexer_name}</span>
          <span className="px-1 py-px rounded-[2px] text-[10px] uppercase bg-[#1a1a1a] border border-[#2a2a2a] text-neutral-300 flex-shrink-0">
            {r.protocol}
          </span>
          <ReleaseBadges release={r} />
        </div>
        <span className="hidden sm:block text-xs font-mono text-neutral-300 truncate" title={r.parsed_quality}>
          {r.parsed_quality || '-'}
        </span>
        <span className="hidden sm:block text-xs font-mono text-neutral-300">{formatBytes(r.size_bytes)}</span>
        <span className="hidden sm:block text-xs font-mono text-neutral-300">
          {isTorrent ? (r.seeders ?? '-') : '-'}
        </span>
        <span className="hidden sm:block text-xs font-mono text-neutral-300">{formatAge(r.publish_date)}</span>
        <span className="hidden sm:block text-xs font-mono text-neutral-200" title="Score (format score)">
          {r.score}
          {r.format_score !== 0 && <span className="text-neutral-500"> ({r.format_score})</span>}
        </span>
        <div className="flex items-center justify-between gap-2 sm:justify-end">
          <span className="sm:hidden text-xs font-mono text-neutral-400 truncate">
            {[r.parsed_quality, formatBytes(r.size_bytes), isTorrent && r.seeders != null ? `${r.seeders} seeds` : null]
              .filter((p): p is string => Boolean(p))
              .join(' · ')}
          </span>
          {grabButton}
        </div>
      </div>
      {expanded && (
        <div id={detailId} className="px-3 pb-3 pt-1 bg-[#0d0d0d] text-xs font-mono space-y-2">
          {blocklistNote(r) && <p className="text-[#ef4444]/90 break-words">{blocklistNote(r)}</p>}
          <div>
            <div className="text-neutral-500 uppercase tracking-wider text-[10px] mb-1">Rejection reasons</div>
            {r.rejection_reasons.length > 0 ? (
              <ul className="list-disc pl-4 text-[#ef4444]/90 space-y-0.5">
                {r.rejection_reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            ) : (
              <p className="text-neutral-400">None</p>
            )}
          </div>
          {breakdown.length > 0 && (
            <div>
              <div className="text-neutral-500 uppercase tracking-wider text-[10px] mb-1">Score breakdown</div>
              <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-3 gap-y-0.5">
                {breakdown.map(([k, v]) => (
                  <React.Fragment key={k}>
                    <dt className="text-neutral-500">{k}</dt>
                    <dd className="text-neutral-300 break-words">{formatValue(v)}</dd>
                  </React.Fragment>
                ))}
              </dl>
            </div>
          )}
        </div>
      )}
    </li>
  );
});
ReleaseRow.displayName = 'ReleaseRow';

/** Interactive release search: every indexer is queried, results are ranked, and the admin picks one to grab. */
export const ManualSearchModal: React.FC<ManualSearchModalProps> = ({ target, onClose, onToast, onGrabbed }) => {
  const search = useManualSearch(target, onToast, onGrabbed);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [tab, setTab] = useState<'releases' | 'history'>('releases');
  const [historyKey, setHistoryKey] = useState<number>(0);
  const uid = useId();
  const sortId = `${uid}-sort`;
  const what = target ? target.title || target.album || '' : '';
  const subtitle = target
    ? `${target.artist}${what ? ` • ${what}` : ''}${search.profileName ? ` · Profile: ${search.profileName}` : ''}`
    : undefined;

  const trackId = target?.trackId != null ? String(target.trackId) : null;
  const albumId = target?.albumId != null ? String(target.albumId) : null;
  const historyEntity: 'track' | 'album' | null = trackId !== null ? 'track' : albumId !== null ? 'album' : null;
  const historyId = trackId ?? albumId;
  const activeTab = historyEntity === null ? 'releases' : tab;

  useEffect(() => {
    setTab('releases');
  }, [target]);

  const toggle = (id: string): void => setExpandedId((cur) => (cur === id ? null : id));

  return (
    <ObsidianModal
      isOpen={target !== null}
      onClose={onClose}
      title="Manual Search"
      subtitle={subtitle}
      maxWidth="sm:max-w-5xl"
      footer={
        <TapeDeckButton size="sm" onClick={onClose}>
          Close
        </TapeDeckButton>
      }
    >
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          {historyEntity !== null && (
            <TabStrip aria-label="Manual search sections">
              <TapeDeckButton size="sm" active={activeTab === 'releases'} onClick={() => setTab('releases')}>
                Releases
              </TapeDeckButton>
              <TapeDeckButton size="sm" active={activeTab === 'history'} onClick={() => setTab('history')}>
                History
              </TapeDeckButton>
            </TabStrip>
          )}
          {historyEntity !== null && historyId !== null && (
            <RejectCurrentButton
              entity={historyEntity}
              entityId={historyId}
              onToast={onToast}
              onDone={() => {
                search.rerun();
                setHistoryKey((k) => k + 1);
              }}
            />
          )}
        </div>
        <p className="text-xs font-mono text-neutral-500">
          {search.lastSearch
            ? `Last automatic search: ${historyRelativeTime(search.lastSearch.at)} \u2014 ${search.lastSearch.message}`
            : 'No automatic search recorded'}
        </p>
        {activeTab === 'history' && historyEntity !== null && historyId !== null ? (
          <ItemHistoryTimeline key={`${historyEntity}:${historyId}:${historyKey}`} entity={historyEntity} entityId={historyId} />
        ) : (
        <>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-mono text-neutral-400 mr-auto" aria-live="polite">
            {search.loading ? 'Searching' : `${search.results.length} of ${search.total} releases`}
          </span>
          <div role="group" aria-label="Filter releases" className="tape-transport-bay flex items-center gap-1">
            {FILTERS.map((f) => (
              <TapeDeckButton
                key={f.value}
                size="sm"
                active={search.filter === f.value}
                onClick={() => search.setFilter(f.value)}
              >
                {f.label}
              </TapeDeckButton>
            ))}
          </div>
          <label htmlFor={sortId} className="sr-only">
            Sort releases
          </label>
          <select
            id={sortId}
            name="manual-search-sort"
            value={search.sort}
            onChange={(e) => search.setSort(e.target.value as ManualSearchSort)}
            className="h-9 sm:h-8 px-2 text-[13px] font-mono bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] text-neutral-200"
          >
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          <TapeDeckButton
            size="sm"
            disabled={search.loading}
            onClick={search.rerun}
            icon={<RefreshCw className={`h-3.5 w-3.5 ${search.loading ? 'animate-spin' : ''}`} />}
            aria-label="Re-run search"
            title="Re-run search"
            collapseLabel
          >
            Re-run
          </TapeDeckButton>
        </div>

        {search.indexerErrors.length > 0 && (
          <div
            role="alert"
            className="flex items-start gap-2 p-2.5 rounded-[3px] border border-[#e5a00d]/40 bg-[#e5a00d]/10 text-xs font-mono text-[#e5a00d]"
          >
            <AlertTriangle className="h-4 w-4 flex-shrink-0 mt-px" />
            <ul className="min-w-0 space-y-0.5">
              {search.indexerErrors.map((e) => (
                <li key={`${e.source}:${e.error}`} className="break-words">
                  {e.source}: {e.error}
                </li>
              ))}
            </ul>
          </div>
        )}

        {search.loading ? (
          <div className="flex flex-col items-center gap-3 py-10">
            <CassetteLoader size="sm" />
            <p className="text-xs font-mono text-neutral-400 text-center px-4">
              Querying all indexers. This can take up to a minute.
            </p>
          </div>
        ) : search.error ? (
          <p role="alert" className="text-xs text-red-300 font-mono py-6 text-center">
            {search.error}
          </p>
        ) : search.results.length === 0 ? (
          <p className="text-xs text-neutral-500 font-mono py-6 text-center">No releases found</p>
        ) : (
          <div className="border border-[#1f1f1f] rounded-[3px] bg-[#0a0a0a]">
            <div
              className={`hidden ${ROW_GRID} px-2.5 py-1.5 border-b border-[#1f1f1f] text-[10px] font-mono uppercase tracking-wider text-neutral-500`}
            >
              <span />
              <span>Release</span>
              <span>Indexer</span>
              <span>Quality</span>
              <span>Size</span>
              <span>Seeds</span>
              <span>Age</span>
              <span>Score</span>
              <span />
            </div>
            <ul>
              {search.results.map((r) => (
                <ReleaseRow
                  key={r.id}
                  release={r}
                  expanded={expandedId === r.id}
                  busy={search.busyIds.has(r.id)}
                  grabbed={search.grabbedIds.has(r.id)}
                  onToggle={toggle}
                  onGrab={(rel) => void search.grab(rel)}
                />
              ))}
            </ul>
          </div>
        )}
        </>
        )}
      </div>
    </ObsidianModal>
  );
};
