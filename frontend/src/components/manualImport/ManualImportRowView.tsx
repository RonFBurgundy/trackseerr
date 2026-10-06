import React, { useEffect, useId, useState } from 'react';
import { Fingerprint, Loader2, Search } from 'lucide-react';
import { useDebouncedValue } from '@/hooks/useDebouncedValue';
import { errorMessage } from '@/services/apiClient';
import { searchManualImportAlbums } from '@/services/manualImportService';
import { TapeDeckButton } from '@/components/ui';
import type { ManualImportAlbumHit, ManualImportRow, MatchStrength } from '@/types/manualImport';

const STRENGTH_CLASS: Record<MatchStrength, string> = {
  strong: 'border-[var(--status-success)]/50 text-[var(--status-success)] bg-[var(--status-success)]/10',
  weak: 'border-[var(--accent-amber)]/50 text-[var(--accent-amber)] bg-[var(--accent-amber)]/10',
  none: 'border-[var(--border-default)] text-[var(--text-muted)] bg-[var(--bg-surface-elevated)]',
};

const SEARCH_DEBOUNCE_MS = 300;
const labelClass = 'block text-[10px] uppercase tracking-widest font-mono text-[var(--text-muted)] mb-1';
const controlClass =
  'w-full min-h-[36px] px-2.5 rounded-[3px] bg-[var(--bg-surface-elevated)] border border-[var(--border-default)] text-[13px] text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-amber)] disabled:opacity-50';

interface AlbumSearchProps {
  onPick: (albumId: string) => void;
  onCancel: () => void;
}

/** Inline album search (debounced) used to repoint one row at a different album. */
const AlbumSearch: React.FC<AlbumSearchProps> = ({ onPick, onCancel }) => {
  const inputId = useId();
  const [query, setQuery] = useState<string>('');
  const debounced = useDebouncedValue(query.trim(), SEARCH_DEBOUNCE_MS);
  const [hits, setHits] = useState<ManualImportAlbumHit[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (debounced.length < 2) {
      setHits([]);
      return undefined;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    searchManualImportAlbums(debounced, controller.signal)
      .then((res) => {
        if (!controller.signal.aborted) setHits(res);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) return;
        setError(errorMessage(err, 'Album search failed'));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [debounced]);

  return (
    <div className="mt-2 p-2 rounded-[3px] border border-[var(--border-default)] bg-[var(--bg-canvas)]">
      <label htmlFor={inputId} className={labelClass}>
        Search albums
      </label>
      <div className="flex items-center gap-1.5">
        <input
          id={inputId}
          name="manual-import-album-search"
          type="search"
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Album or artist"
          className={controlClass}
        />
        <TapeDeckButton size="sm" onClick={onCancel}>
          Cancel
        </TapeDeckButton>
      </div>
      {loading && (
        <p className="mt-1.5 flex items-center gap-1.5 text-[11px] font-mono text-[var(--text-muted)]">
          <Loader2 className="h-3 w-3 animate-spin" /> Searching
        </p>
      )}
      {error && <p className="mt-1.5 text-[11px] font-mono text-[var(--status-error)]">{error}</p>}
      {!loading && !error && debounced.length >= 2 && hits.length === 0 && (
        <p className="mt-1.5 text-[11px] font-mono text-[var(--text-muted)]">No albums found</p>
      )}
      {hits.length > 0 && (
        <ul className="mt-1.5 max-h-40 overflow-y-auto divide-y divide-[var(--border-subtle)]">
          {hits.map((h) => (
            <li key={h.id}>
              <button
                type="button"
                onClick={() => onPick(h.id)}
                className="w-full min-h-[36px] px-2 text-left text-[12px] text-[var(--text-primary)] hover:bg-[var(--bg-card-hover)]"
              >
                {h.title} <span className="text-[var(--text-muted)]">&mdash; {h.artist_name}{h.year ? ` (${h.year})` : ''}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};

export interface ManualImportRowViewProps {
  row: ManualImportRow;
  duplicate: boolean;
  disabled: boolean;
  onToggle: (key: string) => void;
  onSelectTrack: (key: string, trackId: string | null) => void;
  onChangeAlbum: (key: string, albumId: string) => Promise<void>;
  onIdentify: (key: string) => Promise<void>;
}

function tagSummary(row: ManualImportRow): string {
  const t = row.item.tags;
  const track = [t.track_number !== null ? `#${t.track_number}` : null, t.title].filter(Boolean).join(' ');
  return [t.artist, t.album, track].filter((p): p is string => Boolean(p)).join(' – ') || 'No tags';
}

export const ManualImportRowView = React.memo<ManualImportRowViewProps>(function ManualImportRowView({
  row,
  duplicate,
  disabled,
  onToggle,
  onSelectTrack,
  onChangeAlbum,
  onIdentify,
}) {
  const baseId = useId();
  const key = row.item.file_path;
  const [searching, setSearching] = useState<boolean>(false);
  const imported = row.result?.status === 'imported';
  const identifyBusy = row.identify.phase === 'running';

  return (
    <li className={`p-2.5 sm:p-3 rounded-[4px] border bg-[var(--bg-card)] ${duplicate ? 'border-[var(--status-error)]/60' : 'border-[var(--border-default)]'}`}>
      <div className="flex items-start gap-2.5">
        <input
          id={`${baseId}-check`}
          name={`manual-import-select-${key}`}
          type="checkbox"
          aria-label={`Import ${row.item.filename}`}
          checked={row.checked}
          disabled={disabled || imported || row.selectedTrackId === null}
          onChange={() => onToggle(key)}
          className="mt-1 h-4 w-4 shrink-0 accent-[#e5a00d]"
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <p className="min-w-0 break-all text-[13px] font-mono text-[var(--text-primary)]" title={key}>
              {row.item.filename}
            </p>
            <span
              className={`shrink-0 px-1.5 py-0.5 rounded-[2px] border text-[10px] font-bold uppercase tracking-wider ${STRENGTH_CLASS[row.item.match_strength]}`}
            >
              {row.item.match_strength}
            </span>
          </div>
          <p className="mt-0.5 text-[11px] text-[var(--text-secondary)] break-words">{tagSummary(row)}</p>

          <div className="mt-2">
            <label htmlFor={`${baseId}-track`} className={labelClass}>
              Library track
            </label>
            <select
              id={`${baseId}-track`}
              name={`manual-import-track-${key}`}
              value={row.selectedTrackId ?? ''}
              disabled={disabled || imported || row.loadingTracks}
              onChange={(e) => onSelectTrack(key, e.target.value === '' ? null : e.target.value)}
              className={controlClass}
            >
              <option value="">{row.candidates.length === 0 ? 'No candidate tracks' : 'Choose a track'}</option>
              {row.candidates.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.track_number !== null ? `${c.track_number}. ` : ''}
                  {c.title} &mdash; {c.artist_name} / {c.album_title}
                  {c.has_file ? ' (has file)' : ''}
                </option>
              ))}
            </select>
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <TapeDeckButton size="sm" disabled={disabled || imported || row.loadingTracks} onClick={() => setSearching((v) => !v)}>
              Change album&hellip;
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              disabled={disabled || imported || identifyBusy}
              onClick={() => void onIdentify(key)}
              icon={identifyBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Fingerprint className="h-3.5 w-3.5" />}
            >
              Identify
            </TapeDeckButton>
            {row.loadingTracks && (
              <span className="flex items-center gap-1 text-[11px] font-mono text-[var(--text-muted)]">
                <Search className="h-3 w-3" /> Loading tracks
              </span>
            )}
          </div>

          {searching && (
            <AlbumSearch
              onCancel={() => setSearching(false)}
              onPick={(albumId) => {
                setSearching(false);
                void onChangeAlbum(key, albumId);
              }}
            />
          )}

          {(row.identify.phase === 'done' || row.identify.phase === 'failed') && (
            <p
              className={`mt-1.5 text-[11px] font-mono break-words ${
                row.identify.phase === 'done' ? 'text-[var(--text-secondary)]' : 'text-[var(--status-error)]'
              }`}
            >
              {row.identify.message}
            </p>
          )}
          {duplicate && (
            <p role="alert" className="mt-1.5 text-[11px] font-mono text-[var(--status-error)]">
              Another selected file already uses this track. Pick a different track or untick one file.
            </p>
          )}
          {row.result && (
            <p
              className={`mt-1.5 text-[11px] font-mono break-words ${
                imported ? 'text-[var(--status-success)]' : 'text-[var(--status-error)]'
              }`}
            >
              {imported ? 'Imported' : `Failed: ${row.result.error ?? 'unknown error'}`}
            </p>
          )}
        </div>
      </div>
    </li>
  );
});
