import React, { useCallback, useId } from 'react';
import { ArrowLeft, Check, Library, Loader2, Plus, Search, User } from 'lucide-react';
import { ObsidianModal, TapeDeckButton, MonitorOptionSelect } from '@/components/ui';
import { inputClass, labelClass } from '@/components/settings/formClasses';
import { useAddArtist, type SelectedArtist } from '@/hooks/useAddArtist';
import type { MonitorOption } from '@/types/monitoring';

export interface AddArtistDialogProps {
  open: boolean;
  onClose: () => void;
  initialQuery?: string;
  preselected?: SelectedArtist;
  onAdded: (libraryArtistId: string) => void;
}

export const AddArtistDialog: React.FC<AddArtistDialogProps> = ({
  open,
  onClose,
  initialQuery,
  preselected,
  onAdded,
}) => {
  const searchInputId = useId();
  const monitorSelectId = useId();

  const {
    query,
    setQuery,
    results,
    isSearching,
    searchError,
    selectedArtist,
    setSelectedArtist,
    monitorOption,
    setMonitorOption,
    isSubmitting,
    submitError,
    result,
    submit,
    reset,
  } = useAddArtist({ initialQuery, preselected });

  const handleClose = useCallback((): void => {
    reset();
    onClose();
  }, [reset, onClose]);

  const handleSubmit = useCallback(async (): Promise<void> => {
    const artistId = await submit();
    if (artistId && !result?.already_existed) {
      // If ingest returned a brand new artist, trigger onAdded immediately and close
      // Note: if already_existed is true, result is updated and the dialog offers "Open in Library"
      onAdded(artistId);
      handleClose();
    }
  }, [submit, result, onAdded, handleClose]);

  const handleOpenExisting = useCallback((): void => {
    if (result?.id) {
      onAdded(result.id);
      handleClose();
    }
  }, [result, onAdded, handleClose]);

  const alreadyExisted = Boolean(result?.already_existed);

  const footer = (
    <div className="flex w-full items-center justify-between gap-2">
      {selectedArtist && !preselected && !alreadyExisted ? (
        <TapeDeckButton
          size="sm"
          onClick={() => setSelectedArtist(null)}
          disabled={isSubmitting}
          icon={<ArrowLeft className="h-3.5 w-3.5" />}
        >
          Back
        </TapeDeckButton>
      ) : (
        <div />
      )}
      <div className="flex items-center gap-2">
        {alreadyExisted ? (
          <>
            <TapeDeckButton size="sm" onClick={handleClose}>
              Close
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              variant="amber"
              onClick={handleOpenExisting}
              icon={<Library className="h-3.5 w-3.5" />}
            >
              Open in Library
            </TapeDeckButton>
          </>
        ) : selectedArtist ? (
          <>
            <TapeDeckButton size="sm" onClick={handleClose} disabled={isSubmitting}>
              Cancel
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              variant="amber"
              onClick={() => void handleSubmit()}
              disabled={isSubmitting}
              icon={isSubmitting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
            >
              Add artist
            </TapeDeckButton>
          </>
        ) : (
          <TapeDeckButton size="sm" onClick={handleClose}>
            Cancel
          </TapeDeckButton>
        )}
      </div>
    </div>
  );

  return (
    <ObsidianModal
      isOpen={open}
      onClose={handleClose}
      title={selectedArtist ? 'Confirm Artist' : 'Add Artist'}
      subtitle={selectedArtist ? 'Configure monitoring for this artist' : 'Search discovery to add an artist to your library'}
      footer={footer}
      maxWidth="sm:max-w-lg"
    >
      <div className="flex flex-col gap-4">
        {!selectedArtist ? (
          <>
            <div>
              <label htmlFor={searchInputId} className={labelClass}>
                Artist Name
              </label>
              <div className="relative">
                <input
                  id={searchInputId}
                  name="artist-search"
                  type="text"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search artist by name…"
                  autoFocus
                  autoComplete="off"
                  className={`${inputClass} pr-9`}
                />
                <div className="pointer-events-none absolute inset-y-0 right-0 flex items-center pr-3 text-neutral-500">
                  {isSearching ? (
                    <Loader2 className="h-4 w-4 animate-spin text-[#e5a00d]" />
                  ) : (
                    <Search className="h-4 w-4" />
                  )}
                </div>
              </div>
            </div>

            {searchError && (
              <div
                role="alert"
                className="rounded-[3px] border border-red-900/60 bg-[#1a0e0e] px-3 py-2 text-xs font-mono text-red-300"
              >
                {searchError}
              </div>
            )}

            <div className="flex flex-col gap-1.5 min-h-[160px] max-h-[320px] overflow-y-auto pr-1">
              {results.length > 0 ? (
                results.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() =>
                      setSelectedArtist({
                        foreignArtistId: item.id,
                        name: item.title,
                        imageUrl: item.cover_url ?? undefined,
                      })
                    }
                    className="flex w-full items-center gap-3 rounded-[3px] border border-[#222222] bg-[#141414] p-2 text-left transition-colors hover:border-[#e5a00d]/40 hover:bg-[#1a1a1a]"
                  >
                    <div className="relative flex h-10 w-10 shrink-0 items-center justify-center overflow-hidden rounded-[3px] border border-[#2a2a2a] bg-[#0d0d0d]">
                      {item.cover_url ? (
                        <img
                          src={item.cover_url}
                          alt={item.title}
                          width={40}
                          height={40}
                          loading="lazy"
                          decoding="async"
                          className="h-full w-full object-cover"
                        />
                      ) : (
                        <User className="h-5 w-5 text-neutral-600" />
                      )}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-mono text-sm font-bold text-white">{item.title}</div>
                      <div className="truncate font-mono text-[11px] text-neutral-500">{item.id}</div>
                    </div>
                  </button>
                ))
              ) : isSearching ? (
                <div className="flex flex-col items-center justify-center py-10 text-center">
                  <p className="font-mono text-xs text-neutral-400">Searching discovery…</p>
                </div>
              ) : query.trim() ? (
                <div className="flex flex-col items-center justify-center py-10 text-center">
                  <p className="font-mono text-xs text-neutral-400">No artists match your search.</p>
                </div>
              ) : (
                <div className="flex flex-col items-center justify-center py-10 text-center">
                  <p className="font-mono text-xs text-neutral-500">Type an artist name to search discovery.</p>
                </div>
              )}
            </div>
          </>
        ) : (
          <>
            {/* Selected Artist Card */}
            <div className="flex items-center gap-3.5 rounded-[4px] border border-[#2a2a2a] bg-[#141414] p-3">
              <div className="relative flex h-14 w-14 shrink-0 items-center justify-center overflow-hidden rounded-[3px] border border-[#333333] bg-[#0d0d0d]">
                {selectedArtist.imageUrl ? (
                  <img
                    src={selectedArtist.imageUrl}
                    alt={selectedArtist.name}
                    width={56}
                    height={56}
                    loading="lazy"
                    decoding="async"
                    className="h-full w-full object-cover"
                  />
                ) : (
                  <User className="h-7 w-7 text-neutral-600" />
                )}
              </div>
              <div className="min-w-0 flex-1">
                <span className="font-mono text-[10px] uppercase tracking-widest text-[#e5a00d]">Artist</span>
                <h4 className="truncate font-mono text-base font-black text-white">{selectedArtist.name}</h4>
                <p className="truncate font-mono text-xs text-neutral-500">{selectedArtist.foreignArtistId}</p>
              </div>
            </div>

            {/* Already Existed Banner */}
            {alreadyExisted && (
              <div
                role="status"
                className="flex items-center gap-2.5 rounded-[3px] border border-emerald-500/40 bg-emerald-950/30 p-3 text-xs font-mono text-emerald-300"
              >
                <Check className="h-4 w-4 shrink-0 text-emerald-400" />
                <div>
                  <div className="font-bold">Already in library</div>
                  <div className="text-[11px] text-emerald-300/80">
                    This artist is already cataloged. You can open them in your library.
                  </div>
                </div>
              </div>
            )}

            {/* Error Message */}
            {submitError && (
              <div
                role="alert"
                className="rounded-[3px] border border-red-900/60 bg-[#1a0e0e] px-3 py-2 text-xs font-mono text-red-300"
              >
                {submitError}
              </div>
            )}

            {/* Confirm Settings (only show when not already existed) */}
            {!alreadyExisted && (
              <div>
                <MonitorOptionSelect<MonitorOption | 'default'>
                  id={monitorSelectId}
                  name="artist-monitor-option"
                  label="Monitor option"
                  value={monitorOption}
                  onChange={setMonitorOption}
                  disabled={isSubmitting}
                  defaultOptionLabel="Default (from server settings)"
                  defaultOptionValue="default"
                />
                <p className="mt-1 font-mono text-[11px] text-neutral-500">
                  Select which releases to monitor. Leaving at default uses the configured library default.
                </p>
              </div>
            )}
          </>
        )}
      </div>
    </ObsidianModal>
  );
};
