import React, { useId, useState } from 'react';
import { MachinedCard, TapeDeckButton } from '@/components/ui';
import { useServerLogins } from '@/hooks/useServerLogins';
import { errorMessage } from '@/services/apiClient';
import type { AccountMediaServer } from '@/types/account';

export interface ServerLoginsCardProps {
  onToast?: (msg: string, tone?: 'ok' | 'error') => void;
}

const inputClass =
  'w-full h-9 px-3 rounded-[3px] bg-[var(--bg-canvas)] border border-[var(--border-default)] text-[13px] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-[var(--accent-amber)]';

interface ServerLoginAccordionProps {
  server: AccountMediaServer;
  isOpen: boolean;
  isSaving: boolean;
  onToggle: () => void;
  onSave: (serverId: string, username: string, password: string) => Promise<void>;
  onRemove: (serverId: string) => Promise<void>;
}

const ServerLoginAccordion: React.FC<ServerLoginAccordionProps> = ({
  server,
  isOpen,
  isSaving,
  onToggle,
  onSave,
  onRemove,
}) => {
  const baseId = useId();
  const usernameId = `${baseId}-username`;
  const passwordId = `${baseId}-password`;
  const [username, setUsername] = useState<string>(server.username);
  const [password, setPassword] = useState<string>('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password) return;
    await onSave(server.server_id, username.trim(), password);
    setPassword('');
  };

  return (
    <div className="border-t border-[var(--border-subtle)] pt-4 space-y-3 first:border-t-0 first:pt-0">
      <button
        type="button"
        aria-expanded={isOpen}
        onClick={onToggle}
        className="w-full min-h-[36px] flex items-center justify-between gap-3 text-left text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
      >
        <span className="truncate">{server.name}</span>
        <span className="flex items-center gap-3 shrink-0">
          <span className="text-[11px] normal-case tracking-normal text-[var(--text-muted)]">
            {server.saved ? `Saved as ${server.username}` : 'Not set'}
          </span>
          <span className="text-[#e5a00d]">{isOpen ? '−' : '+'}</span>
        </span>
      </button>

      {isOpen && (
        <div className="space-y-3">
          {server.saved && (
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-sm text-[var(--text-primary)]">
              <span>
                Saved as <span className="font-mono text-[#e5a00d]">{server.username}</span>
              </span>
              <TapeDeckButton
                type="button"
                size="sm"
                variant="danger"
                disabled={isSaving}
                onClick={() => void onRemove(server.server_id)}
              >
                Remove
              </TapeDeckButton>
            </div>
          )}

          <form onSubmit={(e) => void handleSubmit(e)} className="space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div className="space-y-1">
                <label
                  htmlFor={usernameId}
                  className="block text-[11px] font-mono uppercase tracking-wider text-[var(--text-secondary)]"
                >
                  Username
                </label>
                <input
                  id={usernameId}
                  name={`server-login-username-${server.server_id}`}
                  type="text"
                  autoComplete="username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className={inputClass}
                />
              </div>

              <div className="space-y-1">
                <label
                  htmlFor={passwordId}
                  className="block text-[11px] font-mono uppercase tracking-wider text-[var(--text-secondary)]"
                >
                  Password
                </label>
                <input
                  id={passwordId}
                  name={`server-login-password-${server.server_id}`}
                  type="password"
                  autoComplete="off"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className={inputClass}
                />
              </div>
            </div>

            <div>
              <TapeDeckButton type="submit" size="sm" disabled={isSaving || !username.trim() || !password}>
                Save
              </TapeDeckButton>
            </div>
          </form>

          <p className="text-xs font-mono text-[var(--text-muted)]">
            Your playlists are written to your own account on this server. The password is stored encrypted.
          </p>
        </div>
      )}
    </div>
  );
};

export const ServerLoginsCard: React.FC<ServerLoginsCardProps> = ({ onToast }) => {
  const { servers, isLoading, isSaving, save, remove } = useServerLogins();
  const [openIds, setOpenIds] = useState<Record<string, boolean>>({});

  if (isLoading || servers.length === 0) {
    return null;
  }

  const toggle = (serverId: string) => {
    setOpenIds((prev) => ({ ...prev, [serverId]: !prev[serverId] }));
  };

  const handleSave = async (serverId: string, username: string, password: string) => {
    try {
      await save(serverId, username, password);
      onToast?.('Media server login saved.', 'ok');
    } catch (err: unknown) {
      onToast?.(errorMessage(err, 'Failed to save media server login'), 'error');
    }
  };

  const handleRemove = async (serverId: string) => {
    try {
      await remove(serverId);
      onToast?.('Media server login removed.', 'ok');
    } catch (err: unknown) {
      onToast?.(errorMessage(err, 'Failed to remove media server login'), 'error');
    }
  };

  return (
    <MachinedCard className="p-3 sm:p-6 space-y-4">
      <h3 className="text-sm font-bold uppercase tracking-wider">Media server logins</h3>
      <div className="space-y-4">
        {servers.map((server) => (
          <ServerLoginAccordion
            key={server.server_id}
            server={server}
            isOpen={Boolean(openIds[server.server_id])}
            isSaving={isSaving}
            onToggle={() => toggle(server.server_id)}
            onSave={handleSave}
            onRemove={handleRemove}
          />
        ))}
      </div>
    </MachinedCard>
  );
};
