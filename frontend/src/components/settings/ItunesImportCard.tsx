import React, { useRef } from 'react';
import { Upload, Plus, Trash2, Loader2 } from 'lucide-react';
import { TapeDeckButton, MachinedCard } from '@/components/ui';
import { useItunesImport } from '@/hooks/useItunesImport';
import { LIST_MONITOR_MODES, LIST_MONITOR_MODE_LABELS, type ListMonitorMode } from '@/types/importLists';
import { inputClass, labelClass } from './formClasses';

const MONITOR_HELP: Readonly<Record<ListMonitorMode, string>> = {
  none: 'Match only. Nothing is searched or requested.',
  track: 'Request each unmatched song.',
  album: 'Monitor the whole album of every unmatched song. Admin only. Can create a very large number of requests.',
  artist: 'Add the artist of every unmatched song. Admin only. Can create a very large number of requests.',
};

const checkClass = 'h-5 w-5 accent-[#e5a00d] shrink-0';

export const ItunesImportCard: React.FC = () => {
  const imp = useItunesImport();
  const fileRef = useRef<HTMLInputElement>(null);

  return (
    <MachinedCard className="p-4 space-y-4">
      <div>
        <h3 className="font-bold text-base text-white">Import iTunes / Apple Music library</h3>
        <p className="mt-1 text-xs font-mono text-neutral-400">
          Export with File &gt; Library &gt; Export Library and upload the XML file. Playlists are imported as snapshots;
          importing the same export again updates them.
        </p>
      </div>

      {imp.error && (
        <div className="text-xs font-mono text-[#ef4444] break-words" role="alert">
          {imp.error}
        </div>
      )}

      {imp.stage === 'upload' && (
        <div>
          <input
            ref={fileRef}
            type="file"
            accept=".xml,application/xml,text/xml"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void imp.upload(file);
              e.target.value = '';
            }}
          />
          <TapeDeckButton
            variant="amber"
            disabled={imp.busy}
            className="w-full sm:w-auto"
            icon={imp.busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
            onClick={() => fileRef.current?.click()}
          >
            {imp.busy ? 'Reading file' : 'Choose library XML'}
          </TapeDeckButton>
        </div>
      )}

      {imp.stage === 'review' && imp.preview && (
        <div className="space-y-4">
          <div className="text-xs font-mono text-neutral-400">
            {imp.preview.track_count.toLocaleString()} tracks. Skipped: {imp.preview.skipped.builtin} built-in,{' '}
            {imp.preview.skipped.folders} folders, {imp.preview.skipped.empty} empty.
          </div>

          <div>
            <div className="flex items-center justify-between gap-2 mb-1.5">
              <span className={labelClass}>Playlists ({imp.selected.size} selected)</span>
              <div className="flex gap-2">
                <TapeDeckButton size="sm" onClick={() => imp.setAll(true)}>All</TapeDeckButton>
                <TapeDeckButton size="sm" onClick={() => imp.setAll(false)}>None</TapeDeckButton>
              </div>
            </div>
            <div className="max-h-80 overflow-y-auto border border-[#2a2a2a] rounded-[3px] divide-y divide-[#222222]">
              {imp.preview.playlists.map((p) => (
                <label key={p.key} className="flex items-center gap-3 px-3 min-h-[44px] cursor-pointer hover:bg-[#1c1c1c]">
                  <input
                    type="checkbox"
                    className={checkClass}
                    checked={imp.selected.has(p.key)}
                    onChange={() => imp.toggle(p.key)}
                  />
                  <span className="min-w-0 flex-1 truncate text-sm text-white" title={p.name}>{p.name}</span>
                  {p.is_smart && (
                    <span className="px-2 py-0.5 rounded-[2px] bg-[#1a1a1a] border border-[#2a2a2a] text-[10px] font-mono uppercase text-[#e5a00d]">
                      Smart
                    </span>
                  )}
                  <span className="text-xs font-mono text-neutral-400 tabular-nums">{p.item_count}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="space-y-2">
            <span className={labelClass}>Path mappings (export path to library path)</span>
            {imp.mappings.length === 0 && (
              <div className="text-xs font-mono text-neutral-500">
                No mapping suggested. Without one, tracks are matched by artist, title and album only.
              </div>
            )}
            {imp.mappings.map((m, i) => {
              const hits = imp.preview?.suggested_mappings.find((s) => s.from === m.from && s.to === m.to)?.sample_matches;
              return (
                <div key={i} className="space-y-1">
                  <div className="grid grid-cols-1 sm:grid-cols-[1fr_1fr_auto] gap-2">
                    <input
                      className={inputClass}
                      aria-label="Export path prefix"
                      placeholder="C:/Users/Aaron/iTunes/iTunes Media/Music"
                      value={m.from}
                      onChange={(e) => imp.setMapping(i, { from: e.target.value })}
                    />
                    <input
                      className={inputClass}
                      aria-label="Library path prefix"
                      placeholder="/music"
                      value={m.to}
                      onChange={(e) => imp.setMapping(i, { to: e.target.value })}
                    />
                    <TapeDeckButton aria-label="Remove mapping" icon={<Trash2 className="h-4 w-4" />} onClick={() => imp.removeMapping(i)} />
                  </div>
                  {hits !== undefined && (
                    <div className="text-[11px] font-mono text-[#22c55e]">{hits} sampled files line up with this mapping</div>
                  )}
                </div>
              );
            })}
            <TapeDeckButton size="sm" icon={<Plus className="h-3.5 w-3.5" />} onClick={imp.addMapping}>
              Add mapping
            </TapeDeckButton>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label htmlFor="itunes-mode" className={labelClass}>Monitor missing tracks</label>
              <select
                id="itunes-mode"
                className={inputClass}
                value={imp.monitorMode}
                onChange={(e) => imp.setMonitorMode(e.target.value as ListMonitorMode)}
              >
                {LIST_MONITOR_MODES.map((m) => (
                  <option key={m} value={m}>{LIST_MONITOR_MODE_LABELS[m]}</option>
                ))}
              </select>
              <p className="mt-1 text-[11px] font-mono text-neutral-500">{MONITOR_HELP[imp.monitorMode]}</p>
            </div>
            <div>
              <label htmlFor="itunes-prefix" className={labelClass}>Name prefix (optional)</label>
              <input
                id="itunes-prefix"
                className={inputClass}
                maxLength={100}
                placeholder="iTunes: "
                value={imp.namePrefix}
                onChange={(e) => imp.setNamePrefix(e.target.value)}
              />
            </div>
          </div>

          <label className="flex items-center gap-3 min-h-[44px] cursor-pointer text-sm text-neutral-200">
            <input type="checkbox" className={checkClass} checked={imp.includeFolders} onChange={(e) => imp.setIncludeFolders(e.target.checked)} />
            Prefix playlist names with their folder ("Folder / Playlist")
          </label>
          <div>
            <label className="flex items-center gap-3 min-h-[44px] text-sm text-neutral-500">
              <input type="checkbox" className={checkClass} checked={imp.importPlayStats} disabled onChange={() => undefined} />
              Import play counts and ratings
            </label>
            <p className="text-[11px] font-mono text-neutral-500">
              Not available: the library keeps no per-track play count or rating store.
            </p>
          </div>

          <div className="flex flex-col sm:flex-row gap-2">
            <TapeDeckButton
              variant="amber"
              className="w-full sm:w-auto"
              disabled={imp.busy || imp.selected.size === 0}
              icon={imp.busy ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
              onClick={() => void imp.start()}
            >
              Import {imp.selected.size} playlist{imp.selected.size === 1 ? '' : 's'}
            </TapeDeckButton>
            <TapeDeckButton className="w-full sm:w-auto" onClick={imp.reset}>Cancel</TapeDeckButton>
          </div>
        </div>
      )}

      {(imp.stage === 'running' || imp.stage === 'done') && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 text-sm text-white">
            {imp.stage === 'running' && <Loader2 className="h-4 w-4 animate-spin text-[#e5a00d]" />}
            {imp.stage === 'running'
              ? `Importing ${imp.status?.done ?? 0} of ${imp.status?.total ?? imp.selected.size}`
              : imp.status?.state === 'failed'
                ? 'Import failed'
                : 'Import complete'}
          </div>
          {imp.status?.error && <div className="text-xs font-mono text-[#ef4444]" role="alert">{imp.status.error}</div>}
          <div className="max-h-80 overflow-y-auto border border-[#2a2a2a] rounded-[3px] divide-y divide-[#222222]">
            {(imp.status?.playlists ?? []).map((p) => (
              <div key={p.key} className="flex items-center gap-3 px-3 min-h-[44px] text-sm">
                <span className="min-w-0 flex-1 truncate text-white" title={p.name}>{p.name}</span>
                {p.state === 'pending' && <span className="text-xs font-mono text-neutral-500">waiting</span>}
                {p.state === 'failed' && <span className="text-xs font-mono text-[#ef4444]">failed</span>}
                {p.state === 'done' && (
                  <span className="text-xs font-mono tabular-nums">
                    <span className="text-[#22c55e]">{p.matched} matched</span>
                    <span className="text-neutral-500"> / </span>
                    <span className="text-neutral-300">{p.missing} missing</span>
                  </span>
                )}
              </div>
            ))}
          </div>
          {imp.stage === 'done' && <TapeDeckButton className="w-full sm:w-auto" onClick={imp.reset}>Import another file</TapeDeckButton>}
        </div>
      )}
    </MachinedCard>
  );
};
