import React, { useState } from 'react';
import { Loader2 } from 'lucide-react';
import { FormField, MachinedCard, StatusMessage, TapeDeckButton, inputClass } from '@/components/ui';
import { PasswordSetupFields, checkPassword } from '@/components/auth';

export interface ChangePasswordFormProps {
  username: string;
  onChange: (current: string, next: string) => Promise<string | null>;
}

export const ChangePasswordForm: React.FC<ChangePasswordFormProps> = ({ username, onChange }) => {
  const [current, setCurrent] = useState<string>('');
  const [next, setNext] = useState<string>('');
  const [confirm, setConfirm] = useState<string>('');
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<boolean>(false);

  const ok = current.length > 0 && checkPassword(next, confirm, username).allOk;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!ok || isSaving) return;
    setIsSaving(true);
    setError(null);
    setSuccess(false);
    try {
      const err = await onChange(current, next);
      if (err) {
        setError(err);
      } else {
        setSuccess(true);
        setCurrent('');
        setNext('');
        setConfirm('');
      }
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <MachinedCard className="p-4 sm:p-6">
      <form onSubmit={handleSubmit} className="space-y-4 max-w-md">
        <h3 className="text-sm font-bold uppercase tracking-wider">Change password</h3>
        <FormField label="Current password" htmlFor="acct-current-pw">
          <input
            id="acct-current-pw"
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            disabled={isSaving}
            className={inputClass}
          />
        </FormField>
        <PasswordSetupFields
          idPrefix="acct"
          passwordLabel="New password"
          username={username}
          password={next}
          confirm={confirm}
          onPasswordChange={setNext}
          onConfirmChange={setConfirm}
          disabled={isSaving}
        />
        {error && <StatusMessage variant="error">{error}</StatusMessage>}
        {success && (
          <StatusMessage variant="success">
            Password changed. Your other sessions were signed out.
          </StatusMessage>
        )}
        <TapeDeckButton
          type="submit"
          variant="amber"
          disabled={!ok || isSaving}
          icon={isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
        >
          Change password
        </TapeDeckButton>
      </form>
    </MachinedCard>
  );
};
