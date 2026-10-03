import React, { useState } from 'react';
import { Loader2 } from 'lucide-react';
import {
  CopyBox,
  FormField,
  ObsidianModal,
  StatusMessage,
  TapeDeckButton,
  inputClass,
} from '@/components/ui';
import { PERMISSION_PRESETS } from '@/types/account';
import type { CreateLocalUserPayload, CreateLocalUserResult } from '@/types/account';
import { errorMessage } from '@/services/apiClient';

export interface CreateUserModalProps {
  isOpen: boolean;
  onClose: () => void;
  onCreate: (payload: CreateLocalUserPayload) => Promise<CreateLocalUserResult>;
}

const USERNAME_RE = /^[a-z0-9._-]{3,32}$/;

export const CreateUserModal: React.FC<CreateUserModalProps> = ({ isOpen, onClose, onCreate }) => {
  const [username, setUsername] = useState<string>('');
  const [email, setEmail] = useState<string>('');
  const [presetId, setPresetId] = useState<string>(PERMISSION_PRESETS[0].id);
  const [isBusy, setIsBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  // The invite link exists only in the creation response; it is held here until closed.
  const [inviteUrl, setInviteUrl] = useState<string | null>(null);

  const close = () => {
    setUsername('');
    setEmail('');
    setPresetId(PERMISSION_PRESETS[0].id);
    setError(null);
    setInviteUrl(null);
    onClose();
  };

  const normalized = username.trim().toLowerCase();
  const valid = USERNAME_RE.test(normalized);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!valid || isBusy) return;
    const preset = PERMISSION_PRESETS.find((p) => p.id === presetId) ?? PERMISSION_PRESETS[0];
    setIsBusy(true);
    setError(null);
    try {
      const res = await onCreate({
        username: normalized,
        ...(email.trim() ? { email: email.trim() } : {}),
        permissions: preset.permissions,
      });
      setInviteUrl(res.invite_url);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to create user'));
    } finally {
      setIsBusy(false);
    }
  };

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={close}
      title={inviteUrl ? 'Invite link' : 'Create local user'}
      maxWidth="sm:max-w-lg"
      footer={
        inviteUrl ? (
          <TapeDeckButton variant="amber" onClick={close}>
            Done
          </TapeDeckButton>
        ) : (
          <>
            <TapeDeckButton type="button" onClick={close}>
              Cancel
            </TapeDeckButton>
            <TapeDeckButton
              type="submit"
              form="create-user-form"
              variant="amber"
              disabled={!valid || isBusy}
              icon={isBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
            >
              Create user
            </TapeDeckButton>
          </>
        )
      }
    >
      {inviteUrl ? (
        <div className="space-y-4">
          <StatusMessage variant="info">
            Copy this link now. It is shown only once and expires in 48 hours. The user opens it to
            set their own password.
          </StatusMessage>
          <CopyBox label="Invite link" value={inviteUrl} />
        </div>
      ) : (
        <form id="create-user-form" onSubmit={submit} className="space-y-4">
          <FormField
            label="Username"
            htmlFor="new-user-name"
            hint="3 to 32 characters: a-z, 0-9, dot, underscore, hyphen. Must be unique, including across Plex users."
          >
            <input
              id="new-user-name"
              type="text"
              autoCapitalize="none"
              spellCheck={false}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className={inputClass}
            />
          </FormField>
          <FormField label="Email (optional)" htmlFor="new-user-email">
            <input
              id="new-user-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className={inputClass}
            />
          </FormField>
          <fieldset className="space-y-2">
            <legend className="text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] mb-1.5">
              Permissions preset
            </legend>
            {PERMISSION_PRESETS.map((p) => (
              <label
                key={p.id}
                className="flex items-center gap-3 min-h-[44px] sm:min-h-0 cursor-pointer text-xs font-mono"
              >
                <input
                  type="radio"
                  name="preset"
                  checked={presetId === p.id}
                  onChange={() => setPresetId(p.id)}
                  className="accent-[var(--accent-amber)]"
                />
                {p.label}
              </label>
            ))}
            <p className="text-[11px] font-mono text-[var(--text-muted)]">
              Fine-tune permissions and quotas after creating the user.
            </p>
          </fieldset>
          {error && <StatusMessage variant="error">{error}</StatusMessage>}
        </form>
      )}
    </ObsidianModal>
  );
};
