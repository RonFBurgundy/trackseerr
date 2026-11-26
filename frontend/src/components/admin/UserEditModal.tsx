import React, { useEffect, useId, useState } from 'react';
import { Loader2 } from 'lucide-react';
import {
  CopyBox,
  FormField,
  MachinedCard,
  ObsidianModal,
  StatusMessage,
  TactileSwitch,
  TapeDeckButton,
  inputClass,
} from '@/components/ui';
import {
  PERMISSION_ALL_KNOWN_BITS,
  PERMISSION_ADMIN,
  QUOTA_KINDS,
  QUOTA_LABELS,
} from '@/types/account';
import type { AdminUser, QuotaKind, QuotaOverrides, UpdateUserPayload } from '@/types/account';
import type { UseAdminUsersReturn } from '@/hooks/useAdminUsers';
import type { UseServerUserLinksReturn } from '@/hooks/useServerUserLinks';
import { errorMessage } from '@/services/apiClient';
import { PermissionSwitches } from './PermissionSwitches';

export interface UserEditModalProps {
  user: AdminUser | null;
  currentUserId: string | number | undefined;
  adminHook: UseAdminUsersReturn;
  serverLinksHook?: UseServerUserLinksReturn;
  onClose: () => void;
}

type OverrideKey = QuotaKind | 'window_days';

interface QuotaOverrideFieldProps {
  label: string;
  value: number | null;
  effective: number | undefined;
  onChange: (v: number | null) => void;
}

const QuotaOverrideField: React.FC<QuotaOverrideFieldProps> = ({
  label,
  value,
  effective,
  onChange,
}) => {
  const useDefault = value === null;
  const fieldId = useId();
  return (
    <div className="space-y-1.5">
      <label
        htmlFor={`${fieldId}-value`}
        className="block text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)]"
      >
        {label}
      </label>
      <input
        id={`${fieldId}-value`}
        name={`quota-${label.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`}
        type="number"
        min={0}
        value={useDefault ? '' : value}
        placeholder={effective !== undefined ? `Default (${effective})` : 'Default'}
        disabled={useDefault}
        onChange={(e) => {
          const n = Number.parseInt(e.target.value, 10);
          onChange(Number.isNaN(n) ? 0 : Math.max(0, n));
        }}
        className={inputClass}
      />
      <div className="flex items-center min-h-[36px] sm:min-h-0 text-[11px] font-mono">
        <TactileSwitch
          id={`${fieldId}-default`}
          name={`quota-${label.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-use-default`}
          label="Use default"
          checked={useDefault}
          onChange={(checked) => onChange(checked ? null : (effective ?? 0))}
        />
      </div>
    </div>
  );
};

/** Two-step button: first click arms it, second click runs the action. No window.confirm. */
const ConfirmAction: React.FC<{
  label: string;
  confirmLabel: string;
  onConfirm: () => Promise<void>;
  disabled?: boolean;
  danger?: boolean;
}> = ({ label, confirmLabel, onConfirm, disabled = false, danger = false }) => {
  const [armed, setArmed] = useState<boolean>(false);
  const [busy, setBusy] = useState<boolean>(false);

  return (
    <TapeDeckButton
      type="button"
      size="sm"
      variant={armed || danger ? 'danger' : 'default'}
      active={armed}
      disabled={disabled || busy}
      onClick={async () => {
        if (!armed) {
          setArmed(true);
          return;
        }
        setBusy(true);
        try {
          await onConfirm();
        } finally {
          setBusy(false);
          setArmed(false);
        }
      }}
      onBlur={() => setArmed(false)}
      icon={busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
    >
      {armed ? confirmLabel : label}
    </TapeDeckButton>
  );
};

const ServerLinkForm: React.FC<{
  serverId: string;
  serverName: string;
  userId: string;
  defaultUsername: string;
  onLink: (serverId: string, userId: string, serverUsername: string) => Promise<void>;
  onSuccess: (msg: string) => void;
  onError: (msg: string | null) => void;
}> = ({ serverId, serverName, userId, defaultUsername, onLink, onSuccess, onError }) => {
  const fieldId = useId();
  const [value, setValue] = useState<string>(defaultUsername);
  const [busy, setBusy] = useState<boolean>(false);

  useEffect(() => {
    setValue(defaultUsername);
  }, [defaultUsername, userId]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = value.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    onError(null);
    try {
      await onLink(serverId, userId, trimmed);
      onSuccess(`Linked to ${serverName}.`);
    } catch (err: unknown) {
      onError(errorMessage(err, `Failed to link to ${serverName}`));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="flex flex-col sm:flex-row sm:items-end gap-2">
      <div className="flex-1">
        <FormField label={`Link to ${serverName}`} htmlFor={fieldId}>
          <input
            id={fieldId}
            name={`server-link-${serverId}`}
            type="text"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={`${serverName} username`}
            autoCapitalize="none"
            spellCheck={false}
            className={inputClass}
          />
        </FormField>
      </div>
      <TapeDeckButton
        type="submit"
        size="sm"
        variant="amber"
        disabled={!value.trim() || busy}
        icon={busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
      >
        Save
      </TapeDeckButton>
    </form>
  );
};

export const UserEditModal: React.FC<UserEditModalProps> = ({
  user,
  currentUserId,
  adminHook,
  serverLinksHook,
  onClose,
}) => {
  const [permissions, setPermissions] = useState<number>(0);
  const [email, setEmail] = useState<string>('');
  const [overrides, setOverrides] = useState<QuotaOverrides>({
    tracks: null,
    albums: null,
    discographies: null,
    window_days: null,
  });
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [resetUrl, setResetUrl] = useState<string | null>(null);
  const [deleteOpen, setDeleteOpen] = useState<boolean>(false);
  const [deleteName, setDeleteName] = useState<string>('');
  const [deleteBusy, setDeleteBusy] = useState<boolean>(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [unlinkingServerId, setUnlinkingServerId] = useState<string | null>(null);

  const userId = user?.id;
  useEffect(() => {
    if (!user) return;
    setPermissions(user.permissions & PERMISSION_ALL_KNOWN_BITS);
    setEmail(user.email ?? '');
    setOverrides(user.overrides);
    setError(null);
    setNotice(null);
    setResetUrl(null);
    // Re-seed only when a different user is opened or the server copy changes identity.
  }, [userId]);

  if (!user) return null;

  const isSelf = String(currentUserId) === user.id;
  const isLocal = user.auth_type === 'local';
  const userIsAdmin = (permissions & PERMISSION_ADMIN) !== 0;

  const run = async (fn: () => Promise<void>, okMsg: string) => {
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(okMsg);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Action failed'));
    }
  };

  const setOverride = (key: OverrideKey, v: number | null) => {
    setOverrides((prev) => ({ ...prev, [key]: v }));
  };

  const handleSave = async () => {
    setIsSaving(true);
    const payload: UpdateUserPayload = {
      quota_tracks: overrides.tracks,
      quota_albums: overrides.albums,
      quota_discographies: overrides.discographies,
      quota_window_days: overrides.window_days,
      email: email.trim() ? email.trim() : null,
    };
    // Never send an admin-bit change for yourself; the server forbids it.
    if (!isSelf) {
      payload.permissions = permissions;
    }
    try {
      await run(() => adminHook.update(user.id, payload), 'Changes saved.');
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async () => {
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await adminHook.remove(user.id, deleteName);
      setDeleteOpen(false);
      setDeleteName('');
      onClose();
    } catch (err: unknown) {
      setDeleteError(errorMessage(err, 'Failed to delete user'));
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <>
      <ObsidianModal
        isOpen
        onClose={onClose}
        title={user.username}
        subtitle={`${isLocal ? 'Local' : user.auth_type === 'jellyfin' ? 'Jellyfin' : 'Plex'} user${isSelf ? ' (you)' : ''}`}
        maxWidth="sm:max-w-3xl"
        footer={
          <>
            <TapeDeckButton type="button" onClick={onClose}>
              Close
            </TapeDeckButton>
            <TapeDeckButton
              type="button"
              variant="amber"
              onClick={handleSave}
              disabled={isSaving}
              icon={isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
            >
              Save changes
            </TapeDeckButton>
          </>
        }
      >
        <div className="space-y-5">
          {error && <StatusMessage variant="error">{error}</StatusMessage>}
          {notice && <StatusMessage variant="success">{notice}</StatusMessage>}

          <FormField label="Email" htmlFor="edit-user-email">
            <input
              id="edit-user-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className={inputClass}
            />
          </FormField>

          <fieldset>
            <legend className="text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] mb-2">
              Permissions
            </legend>
            <PermissionSwitches
              idPrefix="edit-user-permission"
              value={permissions}
              onChange={setPermissions}
              lockedBits={isSelf ? PERMISSION_ADMIN : 0}
              allOnBits={userIsAdmin ? PERMISSION_ALL_KNOWN_BITS & ~PERMISSION_ADMIN : 0}
              tooltipFor={(bit) =>
                bit === PERMISSION_ADMIN && isSelf
                  ? 'You cannot change your own admin permission'
                  : userIsAdmin && bit !== PERMISSION_ADMIN
                    ? 'Administrators hold every permission'
                    : undefined
              }
            />
            {userIsAdmin && (
              <p className="text-[11px] font-mono text-[var(--text-muted)] mt-1">
                Administrators hold every permission.
              </p>
            )}
          </fieldset>

          <fieldset>
            <legend className="text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] mb-2">
              Quota overrides
            </legend>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {QUOTA_KINDS.map((kind) => (
                <QuotaOverrideField
                  key={kind}
                  label={QUOTA_LABELS[kind]}
                  value={overrides[kind]}
                  effective={user.quotas?.[kind]}
                  onChange={(v) => setOverride(kind, v)}
                />
              ))}
              <QuotaOverrideField
                label="Window (days)"
                value={overrides.window_days}
                effective={user.quotas?.window_days}
                onChange={(v) => setOverride('window_days', v === null ? null : Math.max(1, v))}
              />
            </div>
            {user.usage && (
              <p className="text-[11px] font-mono text-[var(--text-muted)] mt-2">
                Used in window: {user.usage.tracks} tracks, {user.usage.albums} albums,{' '}
                {user.usage.discographies} discographies
              </p>
            )}
          </fieldset>

          {serverLinksHook && serverLinksHook.groups.length > 0 && (() => {
            const linkedGroups = serverLinksHook.groups.filter((g) =>
              g.users.some((u) => u.user_id === user.id)
            );
            const unlinkedGroups = serverLinksHook.groups.filter(
              (g) => !g.users.some((u) => u.user_id === user.id)
            );
            return (
              <MachinedCard className="p-3 sm:p-4 space-y-3">
                <h4 className="text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)]">
                  Media server links
                </h4>
                {linkedGroups.length > 0 ? (
                  <div className="space-y-1">
                    {linkedGroups.map((g) => {
                      const entry = g.users.find((u) => u.user_id === user.id);
                      return (
                        <div
                          key={g.server_id}
                          className="flex items-center justify-between gap-2 py-1 border-b border-[var(--border-subtle)] last:border-0"
                        >
                          <div className="text-xs font-mono">
                            <span className="text-[var(--text-primary)]">{g.server_name}</span>
                            <span className="text-[var(--text-muted)]">
                              {' '}
                              — {entry?.server_username}
                            </span>
                          </div>
                          {!entry?.synthetic && (
                            <TapeDeckButton
                              type="button"
                              size="sm"
                              disabled={unlinkingServerId === g.server_id}
                              onClick={async () => {
                                setUnlinkingServerId(g.server_id);
                                try {
                                  await run(
                                    () => serverLinksHook.unlinkUser(g.server_id, user.id),
                                    `Unlinked from ${g.server_name}.`
                                  );
                                } finally {
                                  setUnlinkingServerId(null);
                                }
                              }}
                            >
                              Unlink
                            </TapeDeckButton>
                          )}
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <p className="text-[11px] font-mono text-[var(--text-muted)]">
                    No media server links.
                  </p>
                )}
                {unlinkedGroups.length > 0 && (
                  <div className="space-y-3 pt-2 border-t border-[var(--border-subtle)]">
                    {unlinkedGroups.map((g) => (
                      <ServerLinkForm
                        key={g.server_id}
                        serverId={g.server_id}
                        serverName={g.server_name}
                        userId={user.id}
                        defaultUsername={user.username}
                        onLink={serverLinksHook.linkUser}
                        onSuccess={(msg) => setNotice(msg)}
                        onError={(msg) => {
                          setError(msg);
                          if (msg) setNotice(null);
                        }}
                      />
                    ))}
                  </div>
                )}
              </MachinedCard>
            );
          })()}

          <MachinedCard className="p-3 sm:p-4 space-y-3">
            <h4 className="text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)]">
              Account actions
            </h4>
            <div className="flex flex-wrap gap-2">
              {!isSelf && (
                <ConfirmAction
                  label={user.disabled ? 'Enable' : 'Disable'}
                  confirmLabel={user.disabled ? 'Confirm enable' : 'Confirm disable'}
                  onConfirm={() =>
                    run(
                      () => adminHook.setDisabled(user.id, !user.disabled),
                      user.disabled ? 'User enabled.' : 'User disabled and signed out.'
                    )
                  }
                />
              )}
              {isLocal && (
                <ConfirmAction
                  label="Reset password"
                  confirmLabel="Confirm reset"
                  onConfirm={async () => {
                    setError(null);
                    setNotice(null);
                    try {
                      setResetUrl(await adminHook.resetPassword(user.id));
                    } catch (err: unknown) {
                      setError(errorMessage(err, 'Failed to reset password'));
                    }
                  }}
                />
              )}
              {isLocal && user.mfa_enabled && (
                <ConfirmAction
                  label="Reset MFA"
                  confirmLabel="Confirm MFA reset"
                  onConfirm={() => run(() => adminHook.resetMfa(user.id), 'MFA reset.')}
                />
              )}
              <ConfirmAction
                label="Sign out everywhere"
                confirmLabel="Confirm sign out"
                onConfirm={() =>
                  run(() => adminHook.revokeSessions(user.id), 'All sessions revoked.')
                }
              />
              {!isSelf && (
                <TapeDeckButton
                  type="button"
                  size="sm"
                  variant="danger"
                  onClick={() => {
                    setDeleteName('');
                    setDeleteError(null);
                    setDeleteOpen(true);
                  }}
                >
                  Delete user
                </TapeDeckButton>
              )}
            </div>
            {resetUrl && (
              <div className="space-y-2">
                <StatusMessage variant="info">
                  Password reset link. Shown only once and expires in 48 hours.
                </StatusMessage>
                <CopyBox value={resetUrl} />
              </div>
            )}
          </MachinedCard>
        </div>
      </ObsidianModal>

      <ObsidianModal
        isOpen={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        title="Delete user"
        maxWidth="sm:max-w-md"
        footer={
          <>
            <TapeDeckButton type="button" onClick={() => setDeleteOpen(false)}>
              Cancel
            </TapeDeckButton>
            <TapeDeckButton
              type="button"
              variant="danger"
              disabled={deleteName !== user.username || deleteBusy}
              onClick={handleDelete}
              icon={deleteBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
            >
              Delete permanently
            </TapeDeckButton>
          </>
        }
      >
        <div className="space-y-4">
          <p className="text-xs font-mono text-[var(--text-secondary)]">
            This permanently deletes the user and their data. Type{' '}
            <span className="text-[var(--text-primary)] font-bold">{user.username}</span> to
            confirm.
          </p>
          <input
            id="delete-user-confirm"
            name="delete-user-confirm"
            type="text"
            aria-label="Type the username to confirm"
            autoCapitalize="none"
            spellCheck={false}
            value={deleteName}
            onChange={(e) => setDeleteName(e.target.value)}
            className={inputClass}
          />
          {deleteError && <StatusMessage variant="error">{deleteError}</StatusMessage>}
        </div>
      </ObsidianModal>
    </>
  );
};
