import React, { useEffect, useId, useMemo, useState } from 'react';
import { Loader2, Plus } from 'lucide-react';
import { ObsidianModal, TabStrip, TapeDeckButton, TactileSwitch, ActionBar } from '@/components/ui';
import { inputClass, labelClass } from '@/components/settings/formClasses';
import { routeToHash, settingsRouteFor } from '@/hooks/useAppRoute';
import { useListeningSources } from '@/hooks/useListeningSources';
import {
  AUTO_REQUEST_DENIED_REASON,
  LISTENING_PROVIDERS,
  LISTENING_PROVIDER_LABELS,
  type ListeningProvider,
  type ListeningProviderSources,
  type ListeningSourceItem,
} from '@/types/listening';

export interface ListeningPlaylistModalProps {
  isOpen: boolean;
  onClose: () => void;
  /** Admin or holder of the auto-request permission. */
  canAutoRequest: boolean;
  /** Called after a playlist was created so the list can reload. */
  onCreated: () => Promise<void>;
}

const LB_GROUP_LABELS: Record<string, string> = { created_for: 'Made for you', playlist: 'Your playlists' };
const itemKey = (item: ListeningSourceItem): string => `${item.kind}:${item.ref}`;

const ScrobblingLink: React.FC<{ onNavigate: () => void }> = ({ onNavigate }) => (
  <a
    href={routeToHash(settingsRouteFor('requests', 'scrobbling'))}
    onClick={onNavigate}
    className="text-[var(--accent-amber)] hover:underline"
  >
    Open Settings &rsaquo; Scrobbling
  </a>
);

interface ProviderFormProps {
  provider: ListeningProvider;
  info: ListeningProviderSources;
  canAutoRequest: boolean;
  isCreating: boolean;
  error: string | null;
  onSubmit: (item: ListeningSourceItem, keepInSync: boolean, autoRequest: boolean) => void;
  onNavigate: () => void;
}

const ProviderForm: React.FC<ProviderFormProps> = ({
  provider,
  info,
  canAutoRequest,
  isCreating,
  error,
  onSubmit,
  onNavigate,
}) => {
  const uid = useId();
  const lastfm = provider === 'lastfm';
  const [kind, setKind] = useState<string>('loved');
  const [periodKey, setPeriodKey] = useState<string>('');
  const [listKey, setListKey] = useState<string>('');
  const [keepInSync, setKeepInSync] = useState<boolean>(true);
  const [autoRequest, setAutoRequest] = useState<boolean>(false);

  const periods = useMemo(() => info.lists.filter((l) => l.kind === 'top_tracks'), [info.lists]);
  const selected: ListeningSourceItem | undefined = lastfm
    ? kind === 'loved'
      ? info.lists.find((l) => l.kind === 'loved')
      : (periods.find((p) => p.ref === periodKey) ?? periods[0])
    : (info.lists.find((l) => itemKey(l) === listKey) ?? info.lists[0]);

  const grouped = useMemo(() => {
    const groups = new Map<string, ListeningSourceItem[]>();
    for (const item of info.lists) groups.set(item.kind, [...(groups.get(item.kind) ?? []), item]);
    return groups;
  }, [info.lists]);

  if (!info.linked || !info.available) {
    return (
      <div className="p-3 bg-[#161616] border border-[#222222] rounded-[3px] space-y-2 text-xs text-neutral-300 font-mono">
        <p>{info.reason ?? `${LISTENING_PROVIDER_LABELS[provider]} is not available.`}</p>
        {!info.linked && <ScrobblingLink onNavigate={onNavigate} />}
      </div>
    );
  }

  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (selected) onSubmit(selected, keepInSync, canAutoRequest && autoRequest);
      }}
    >
      <p className="text-[11px] text-neutral-500 font-mono">
        Linked as <span className="text-neutral-300">{info.username}</span>
      </p>

      {lastfm ? (
        <>
          <div>
            <label htmlFor={`${uid}-kind`} className={labelClass}>
              List
            </label>
            <select
              id={`${uid}-kind`}
              name="listening-kind"
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              className={inputClass}
            >
              <option value="loved">Loved tracks</option>
              <option value="top_tracks">Top tracks</option>
            </select>
          </div>
          {kind === 'top_tracks' && (
            <div>
              <label htmlFor={`${uid}-period`} className={labelClass}>
                Period
              </label>
              <select
                id={`${uid}-period`}
                name="listening-period"
                value={selected?.ref ?? ''}
                onChange={(e) => setPeriodKey(e.target.value)}
                className={inputClass}
              >
                {periods.map((p) => (
                  <option key={p.ref} value={p.ref}>
                    {p.label.replace('Top tracks - ', '')}
                  </option>
                ))}
              </select>
            </div>
          )}
        </>
      ) : (
        <div>
          <label htmlFor={`${uid}-list`} className={labelClass}>
            Playlist
          </label>
          <select
            id={`${uid}-list`}
            name="listening-list"
            value={selected ? itemKey(selected) : ''}
            onChange={(e) => setListKey(e.target.value)}
            className={inputClass}
          >
            {[...grouped.entries()].map(([groupKind, items]) => (
              <optgroup key={groupKind} label={LB_GROUP_LABELS[groupKind] ?? groupKind}>
                {items.map((item) => (
                  <option key={itemKey(item)} value={itemKey(item)}>
                    {item.label}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          {info.lists.length === 0 && (
            <p className="text-[11px] text-neutral-500 font-mono mt-1">No playlists found on this account yet.</p>
          )}
        </div>
      )}

      <div className="space-y-3 pt-3 border-t border-[#1f1f1f]">
        <div>
          <TactileSwitch
            id={`${uid}-sync`}
            name="keep-in-sync"
            label="Keep in sync"
            checked={keepInSync}
            onChange={setKeepInSync}
          />
          <p className="text-[11px] text-neutral-500 font-mono mt-1">
            Refresh the playlist on every sync. Off keeps a one-time snapshot.
          </p>
        </div>
        <div>
          <TactileSwitch
            id={`${uid}-auto`}
            name="auto-request"
            label="Auto-request missing tracks"
            checked={canAutoRequest && autoRequest}
            onChange={setAutoRequest}
            disabled={!canAutoRequest}
            title={canAutoRequest ? undefined : AUTO_REQUEST_DENIED_REASON}
          />
          <p className="text-[11px] text-neutral-500 font-mono mt-1">
            {canAutoRequest
              ? 'Requests count against your quota and follow normal approval. Off lists missing tracks only.'
              : AUTO_REQUEST_DENIED_REASON}
          </p>
        </div>
      </div>

      {error && (
        <p role="alert" className="text-xs text-[var(--status-error)] font-mono">
          {error}
        </p>
      )}

      <ActionBar align="end" className="pt-2">
        <TapeDeckButton
          type="submit"
          variant="amber"
          size="md"
          disabled={isCreating || !selected}
          icon={isCreating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
        >
          {isCreating ? 'Creating...' : 'Create Playlist'}
        </TapeDeckButton>
      </ActionBar>
    </form>
  );
};

/** "From your listening": builds a playlist from the user's own linked Last.fm or ListenBrainz account. */
export const ListeningPlaylistModal: React.FC<ListeningPlaylistModalProps> = ({
  isOpen,
  onClose,
  canAutoRequest,
  onCreated,
}) => {
  const { sources, isLoading, isCreating, error: loadError, create } = useListeningSources(isOpen);
  const [provider, setProvider] = useState<ListeningProvider>('lastfm');
  const [createError, setCreateError] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen) setCreateError(null);
  }, [isOpen, provider]);

  const handleSubmit = async (item: ListeningSourceItem, keepInSync: boolean, autoRequest: boolean) => {
    const message = await create({
      provider,
      kind: item.kind,
      ref: item.ref,
      keep_in_sync: keepInSync,
      auto_request: autoRequest,
    });
    if (message) {
      setCreateError(message);
      return;
    }
    await onCreated();
    onClose();
  };

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="From Your Listening"
      subtitle="Build a playlist from your linked Last.fm or ListenBrainz account"
    >
      <div className="space-y-4">
        <TabStrip fill>
          {LISTENING_PROVIDERS.map((p) => (
            <TapeDeckButton key={p} size="sm" active={provider === p} onClick={() => setProvider(p)}>
              {LISTENING_PROVIDER_LABELS[p]}
            </TapeDeckButton>
          ))}
        </TabStrip>

        {isLoading && (
          <div className="flex items-center gap-2 py-6 justify-center text-xs font-mono text-neutral-400">
            <Loader2 className="h-4 w-4 animate-spin text-[var(--accent-amber)]" />
            Loading your accounts...
          </div>
        )}
        {!isLoading && loadError && (
          <p role="alert" className="text-xs text-[var(--status-error)] font-mono">
            {loadError}
          </p>
        )}
        {!isLoading && sources && (
          <ProviderForm
            key={provider}
            provider={provider}
            info={sources[provider]}
            canAutoRequest={canAutoRequest}
            isCreating={isCreating}
            error={createError}
            onSubmit={(item, keep, auto) => void handleSubmit(item, keep, auto)}
            onNavigate={onClose}
          />
        )}
      </div>
    </ObsidianModal>
  );
};
