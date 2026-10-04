import { useCallback, useEffect, useRef, useState } from 'react';

export type ToastTone = 'ok' | 'error';
export interface ToastState {
  message: string;
  tone: ToastTone;
}

export interface UseToastReturn {
  toast: ToastState | null;
  showToast: (message: string, tone?: ToastTone) => void;
}

export function useToast(): UseToastReturn {
  const [toast, setToast] = useState<ToastState | null>(null);
  const timer = useRef<number | null>(null);

  const showToast = useCallback((message: string, tone: ToastTone = 'ok') => {
    setToast({ message, tone });
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setToast(null), tone === 'error' ? 5000 : 3000);
  }, []);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    []
  );

  return { toast, showToast };
}
