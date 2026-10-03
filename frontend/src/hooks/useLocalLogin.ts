import { useState, useCallback } from 'react';
import { localLogin } from '@/services/localAuthService';

export type LocalLoginStep = 'credentials' | 'mfa';

export interface UseLocalLoginReturn {
  step: LocalLoginStep;
  username: string;
  password: string;
  code: string;
  useRecoveryCode: boolean;
  isSubmitting: boolean;
  error: string | null;
  setUsername: (v: string) => void;
  setPassword: (v: string) => void;
  setCode: (v: string) => void;
  setUseRecoveryCode: (v: boolean) => void;
  submit: () => Promise<void>;
  /** Return to the username/password step (e.g. after a bad code). */
  backToCredentials: () => void;
}

export interface LocalLoginCallbacks {
  /** Called after the server accepted the sign-in. */
  onSignedIn: (opts: { mfaEnrollmentRequired: boolean }) => void | Promise<void>;
}

export function useLocalLogin({ onSignedIn }: LocalLoginCallbacks): UseLocalLoginReturn {
  const [step, setStep] = useState<LocalLoginStep>('credentials');
  const [username, setUsername] = useState<string>('');
  const [password, setPassword] = useState<string>('');
  const [code, setCode] = useState<string>('');
  const [useRecoveryCode, setUseRecoveryCode] = useState<boolean>(false);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(async () => {
    if (isSubmitting) return;
    setIsSubmitting(true);
    setError(null);
    try {
      const trimmed = code.trim();
      const outcome = await localLogin({
        username: username.trim(),
        password,
        ...(step === 'mfa' && trimmed
          ? useRecoveryCode
            ? { recovery_code: trimmed }
            : { totp_code: trimmed }
          : {}),
      });

      if (outcome.kind === 'mfa_required') {
        setStep('mfa');
        setCode('');
        return;
      }
      if (outcome.kind === 'error') {
        setError(outcome.message);
        return;
      }
      setPassword('');
      setCode('');
      setStep('credentials');
      await onSignedIn({ mfaEnrollmentRequired: outcome.mfaEnrollmentRequired });
    } finally {
      setIsSubmitting(false);
    }
  }, [code, isSubmitting, onSignedIn, password, step, useRecoveryCode, username]);

  const backToCredentials = useCallback(() => {
    setStep('credentials');
    setCode('');
    setError(null);
  }, []);

  return {
    step,
    username,
    password,
    code,
    useRecoveryCode,
    isSubmitting,
    error,
    setUsername,
    setPassword,
    setCode,
    setUseRecoveryCode,
    submit,
    backToCredentials,
  };
}
