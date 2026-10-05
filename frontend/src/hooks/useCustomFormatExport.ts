import { useEffect, useState } from 'react';

export interface UseCustomFormatExportReturn {
  json: string | null;
  loading: boolean;
}

/** Fetches the Lidarr-schema JSON for one format while `formatId` is set (null = closed). */
export function useCustomFormatExport(formatId: number | null, exportText: (id: number) => Promise<string | null>): UseCustomFormatExportReturn {
  const [json, setJson] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(false);

  useEffect(() => {
    if (formatId === null) {
      setJson(null);
      return undefined;
    }
    let cancelled = false;
    setLoading(true);
    void exportText(formatId).then((text) => {
      if (cancelled) return;
      setJson(text);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [formatId, exportText]);

  return { json, loading };
}
