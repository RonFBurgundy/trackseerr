import React from 'react';
import { Loader2, User as UserIcon } from 'lucide-react';
import { FormField, StatusMessage, TapeDeckButton, inputClass } from '@/components/ui';
import type { UseLocalLoginReturn } from '@/hooks/useLocalLogin';

export interface LocalLoginFormProps {
  login: UseLocalLoginReturn;
}

export const LocalLoginForm: React.FC<LocalLoginFormProps> = ({ login }) => {
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    void login.submit();
  };

  const isMfa = login.step === 'mfa';

  return (
    <form onSubmit={handleSubmit} className="space-y-4 text-left">
      {login.error && <StatusMessage variant="error">{login.error}</StatusMessage>}

      {!isMfa ? (
        <>
          <FormField label="Username" htmlFor="local-username">
            <input
              id="local-username"
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              value={login.username}
              onChange={(e) => login.setUsername(e.target.value)}
              disabled={login.isSubmitting}
              className={inputClass}
            />
          </FormField>
          <FormField label="Password" htmlFor="local-password">
            <input
              id="local-password"
              type="password"
              autoComplete="current-password"
              value={login.password}
              onChange={(e) => login.setPassword(e.target.value)}
              disabled={login.isSubmitting}
              className={inputClass}
            />
          </FormField>
        </>
      ) : (
        <>
          <p className="text-xs font-mono text-[var(--text-secondary)]">
            {login.useRecoveryCode
              ? 'Enter one of your recovery codes.'
              : 'Enter the 6-digit code from your authenticator app.'}
          </p>
          <FormField
            label={login.useRecoveryCode ? 'Recovery code' : 'Authentication code'}
            htmlFor="local-code"
          >
            <input
              id="local-code"
              type="text"
              inputMode={login.useRecoveryCode ? 'text' : 'numeric'}
              autoComplete="one-time-code"
              autoFocus
              spellCheck={false}
              value={login.code}
              onChange={(e) => login.setCode(e.target.value)}
              disabled={login.isSubmitting}
              placeholder={login.useRecoveryCode ? 'xxxx-xxxx-xxxx' : '123456'}
              className={inputClass}
            />
          </FormField>
          <div className="flex flex-wrap gap-4 text-[11px] font-mono">
            <button
              type="button"
              className="text-[var(--accent-amber)] hover:underline min-h-[44px] sm:min-h-0"
              onClick={() => {
                login.setUseRecoveryCode(!login.useRecoveryCode);
                login.setCode('');
              }}
            >
              {login.useRecoveryCode ? 'Use authenticator code' : 'Use a recovery code'}
            </button>
            <button
              type="button"
              className="text-[var(--text-secondary)] hover:underline min-h-[44px] sm:min-h-0"
              onClick={login.backToCredentials}
            >
              Back
            </button>
          </div>
        </>
      )}

      <TapeDeckButton
        type="submit"
        size="lg"
        className="w-full"
        disabled={
          login.isSubmitting ||
          (isMfa ? login.code.trim().length === 0 : !login.username.trim() || !login.password)
        }
        icon={
          login.isSubmitting ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <UserIcon className="h-4 w-4" />
          )
        }
      >
        {isMfa ? 'Verify' : 'Sign in'}
      </TapeDeckButton>
    </form>
  );
};
