import React from 'react';
import { Check, Circle } from 'lucide-react';
import { FormField, inputClass } from '@/components/ui';
import {
  PASSWORD_MAX_LENGTH,
  PASSWORD_MIN_LENGTH,
  checkPassword,
  passwordStrength,
} from './passwordPolicy';

export interface PasswordSetupFieldsProps {
  username: string;
  password: string;
  confirm: string;
  onPasswordChange: (v: string) => void;
  onConfirmChange: (v: string) => void;
  passwordLabel?: string;
  idPrefix?: string;
  disabled?: boolean;
}

const Hint: React.FC<{ ok: boolean; children: React.ReactNode }> = ({ ok, children }) => (
  <li
    className={`flex items-center gap-1.5 text-[11px] font-mono ${
      ok ? 'text-[var(--status-success)]' : 'text-[var(--text-muted)]'
    }`}
  >
    {ok ? <Check className="h-3 w-3" /> : <Circle className="h-3 w-3" />}
    {children}
  </li>
);

export const PasswordSetupFields: React.FC<PasswordSetupFieldsProps> = ({
  username,
  password,
  confirm,
  onPasswordChange,
  onConfirmChange,
  passwordLabel = 'Password',
  idPrefix = 'pw',
  disabled = false,
}) => {
  const checks = checkPassword(password, confirm, username);
  const strength = passwordStrength(password);

  return (
    <div className="space-y-4">
      <FormField label={passwordLabel} htmlFor={`${idPrefix}-new`}>
        <input
          id={`${idPrefix}-new`}
          type="password"
          autoComplete="new-password"
          value={password}
          maxLength={PASSWORD_MAX_LENGTH + 32}
          disabled={disabled}
          onChange={(e) => onPasswordChange(e.target.value)}
          className={inputClass}
        />
        <div className="mt-2" aria-live="polite">
          <div className="flex gap-1" aria-hidden="true">
            {[1, 2, 3, 4].map((n) => (
              <span
                key={n}
                className={`h-1.5 flex-1 rounded-[1px] ${
                  strength.score >= n
                    ? strength.score <= 1
                      ? 'bg-[var(--status-error)]'
                      : strength.score >= 3
                      ? 'bg-[var(--status-success)]'
                      : 'bg-[var(--accent-amber)]'
                    : 'bg-[var(--border-default)]'
                }`}
              />
            ))}
          </div>
          <p className="mt-1 text-[11px] font-mono text-[var(--text-muted)]">
            Strength: {strength.label} (a hint only)
          </p>
        </div>
      </FormField>

      <FormField label="Confirm password" htmlFor={`${idPrefix}-confirm`}>
        <input
          id={`${idPrefix}-confirm`}
          type="password"
          autoComplete="new-password"
          value={confirm}
          disabled={disabled}
          onChange={(e) => onConfirmChange(e.target.value)}
          className={inputClass}
        />
      </FormField>

      <ul className="space-y-1">
        <Hint ok={checks.lengthOk}>
          {PASSWORD_MIN_LENGTH} to {PASSWORD_MAX_LENGTH} characters
        </Hint>
        <Hint ok={checks.notContainsUsername}>Does not contain your username</Hint>
        <Hint ok={checks.matches}>Passwords match</Hint>
      </ul>
    </div>
  );
};
