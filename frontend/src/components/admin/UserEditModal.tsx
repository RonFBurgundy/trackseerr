import React, { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import {
  CopyBox,
  FormField,
  MachinedCard,
  ObsidianModal,
  StatusMessage,
  TapeDeckButton,
  inputClass,
} from '@/components/ui';
import {
  PERMISSION_ALL_KNOWN_BITS,
  PERMISSION_ADMIN,
  PERMISSION_FLAGS,
  QUOTA_KINDS,
  QUOTA_LABELS,
} from '@/types/account';
import type { AdminUser, QuotaKind, QuotaOverrides, UpdateUserPayload } from '@/types/account';
import type { UseAdminUsersReturn } from '@/hooks/useAdminUsers';
import { errorMessage } from '@/services/apiClient';

export interface UserEditModalProps {
  user: AdminUser | null;
  currentUserId: string | number | undefined;
  adminHook: UseAdminUsersReturn;
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
  return (
    <div className="space-y-1.5">
      <span className="block text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)]">
        {label}
      </span>
      <input
        type="number"
        min={0}
        aria-label={`${label} override`}
        value={useDefault ? '' : value}
        placeholder={effective !== undefined ? `Default (${effective})` : 'Default'}
        disabled={useDefault}
        onChange={(e) => {
          const n = Number.parseInt(e.target.value, 10);
          onChange(Number.isNaN(n) ? 0 : Math.max(0, n));
        }}
        className={inputClass}
      />
      <label className="flex items-center gap-2 min-h-[44px] sm:min-h-0 text-[11px] font-mono cursor-pointer">
        <input
          type="checkbox"
          checked={useDefault}
          onChange={(e) => onChange(e.target.checked ? null : (effective ?? 0))}
          className="accent-[var(--accent-amber)]"
        />
        Use default
      </label>
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

export const UserEditModal: React.FC<UserEditModalProps> = ({
  user,
  currentUserId,
  adminHook,
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

  const togglePermission = (bit: number, checked: boolean) => {
    setPermissions((prev) => (checked ? prev | bit : prev & ~bit));
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
        subtitle={`${isLocal ? 'Local' : 'Plex'} user${isSelf ? ' (you)' : ''}`}
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
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6">
              {PERMISSION_FLAGS.map((flag) => {
                const lockedAdmin = flag.bit === PERMISSION_ADMIN && isSelf;
                return (
                  <label
                    key={flag.bit}
                    className={`flex items-center gap-3 min-h-[44px] sm:min-h-[32px] text-xs font-mono ${
                      lockedAdmin ? 'opacity-60' : 'cursor-pointer'
                    }`}
                    title={lockedAdmin ? 'You cannot change your own admin permission' : undefined}
                  >
                    <input
                      type="checkbox"
                      checked={(permissions & flag.bit) !== 0}
                      disabled={lockedAdmin}
                      onChange={(e) => togglePermission(flag.bit, e.target.checked)}
                      className="accent-[var(--accent-amber)]"
                    />
                    <span>
                      {flag.label}
                      <span className="text-[var(--text-muted)]"> ({flag.bit})</span>
                    </span>
                  </label>
                );
              })}
            </div>
            {isSelf && (
              <p className="text-[11px] font-mono text-[var(--text-muted)] mt-1">
                Your own permissions cannot be changed here.
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

          <MachinedCard className="p-4 space-y-3">
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
