import { useCallback, useState } from 'react';
import type { CustomFormatImportResult } from '@/types/customFormats';
import type { ImportOutcome } from './useCustomFormats';

export interface UseCustomFormatImportReturn {
  text: string;
  setText: (v: string) => void;
  /** Reads a chosen .json file into the textarea. */
  loadFile: (file: File) => Promise<void>;
  busy: boolean;
  result: CustomFormatImportResult | null;
  error: string | null;
  submit: () => Promise<void>;
}

/** State of the import modal: pasted/uploaded JSON, the request, and the per-entry outcome. */
export function useCustomFormatImport(importText: (text: string) => Promise<ImportOutcome>): UseCustomFormatImportReturn {
  const [text, setText] = useState<string>('');
  const [busy, setBusy] = useState<boolean>(false);
  const [result, setResult] = useState<CustomFormatImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadFile = useCallback(async (file: File): Promise<void> => {
    try {
      setText(await file.text());
      setResult(null);
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? `Could not read the file: ${err.message}` : 'Could not read the file.');
    }
  }, []);

  const submit = useCallback(async (): Promise<void> => {
    setBusy(true);
    setError(null);
    const outcome = await importText(text);
    setBusy(false);
    setResult(outcome.result);
    setError(outcome.error);
  }, [importText, text]);

  return { text, setText, loadFile, busy, result, error, submit };
}
