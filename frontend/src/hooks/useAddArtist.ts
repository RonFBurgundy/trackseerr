import { useCallback, useEffect, useState } from 'react';
import { search } from '@/services/discoveryService';
import { ingestArtist, type IngestArtistInput, type IngestArtistResponse } from '@/services/libraryService';
import type { DiscoveryItem } from '@/types/models';
import type { MonitorOption } from '@/types/monitoring';
import { useDebouncedValue } from '@/hooks/useDebouncedValue';

export interface SelectedArtist {
  foreignArtistId: string;
  name: string;
  imageUrl?: string;
}

export function toForeignArtistId(rawId: string): string {
  const trimmed = rawId.trim();
  if (trimmed.startsWith('deezer:artist:') || trimmed.startsWith('itunes:artist:')) {
    return trimmed;
  }
  if (/^\d+$/.test(trimmed)) {
    return `deezer:artist:${trimmed}`;
  }
  if (trimmed.startsWith('deezer:')) {
    return `deezer:artist:${trimmed.split(':').pop()}`;
  }
  if (trimmed.startsWith('itunes:')) {
    return `itunes:artist:${trimmed.split(':').pop()}`;
  }
  return trimmed;
}

export interface UseAddArtistOptions {
  initialQuery?: string;
  preselected?: SelectedArtist;
}

export interface UseAddArtistReturn {
  query: string;
  setQuery: (q: string) => void;
  results: DiscoveryItem[];
  isSearching: boolean;
  searchError: string | null;
  selectedArtist: SelectedArtist | null;
  setSelectedArtist: (artist: SelectedArtist | null) => void;
  monitorOption: MonitorOption | 'default';
  setMonitorOption: (opt: MonitorOption | 'default') => void;
  isSubmitting: boolean;
  submitError: string | null;
  result: IngestArtistResponse | null;
  submit: () => Promise<string | null>;
  reset: () => void;
}

export function useAddArtist(options: UseAddArtistOptions = {}): UseAddArtistReturn {
  const { initialQuery, preselected } = options;
  const [query, setQuery] = useState(initialQuery ?? '');
  const debouncedQuery = useDebouncedValue(query.trim(), 250);
  const [results, setResults] = useState<DiscoveryItem[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [selectedArtist, setSelectedArtist] = useState<SelectedArtist | null>(preselected ?? null);
  const [monitorOption, setMonitorOption] = useState<MonitorOption | 'default'>('default');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [result, setResult] = useState<IngestArtistResponse | null>(null);

  useEffect(() => {
    if (preselected) {
      setSelectedArtist(preselected);
    }
  }, [preselected]);

  useEffect(() => {
    if (initialQuery && !preselected) {
      setQuery(initialQuery);
    }
  }, [initialQuery, preselected]);

  useEffect(() => {
    if (selectedArtist) return undefined;
    if (!debouncedQuery) {
      setResults([]);
      setIsSearching(false);
      setSearchError(null);
      return undefined;
    }

    let active = true;
    setIsSearching(true);
    setSearchError(null);

    search(debouncedQuery, 'artist')
      .then((items) => {
        if (active) {
          setResults(items);
          setIsSearching(false);
        }
      })
      .catch((err: unknown) => {
        if (active) {
          setResults([]);
          setSearchError(err instanceof Error ? err.message : 'Search failed');
          setIsSearching(false);
        }
      });

    return () => {
      active = false;
    };
  }, [debouncedQuery, selectedArtist]);

  const submit = useCallback(async (): Promise<string | null> => {
    if (!selectedArtist) return null;
    setIsSubmitting(true);
    setSubmitError(null);
    try {
      const foreignId = toForeignArtistId(selectedArtist.foreignArtistId);
      const input: IngestArtistInput = {
        foreign_artist_id: foreignId,
        artist_name: selectedArtist.name,
        monitored: true,
        ...(monitorOption !== 'default' ? { monitor_option: monitorOption } : {}),
      };
      const res = await ingestArtist(input);
      setResult(res);
      return res.id;
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to add artist';
      setSubmitError(msg);
      return null;
    } finally {
      setIsSubmitting(false);
    }
  }, [selectedArtist, monitorOption]);

  const reset = useCallback((): void => {
    setQuery(initialQuery ?? '');
    setSelectedArtist(preselected ?? null);
    setResults([]);
    setIsSearching(false);
    setSearchError(null);
    setMonitorOption('default');
    setIsSubmitting(false);
    setSubmitError(null);
    setResult(null);
  }, [initialQuery, preselected]);

  return {
    query,
    setQuery,
    results,
    isSearching,
    searchError,
    selectedArtist,
    setSelectedArtist,
    monitorOption,
    setMonitorOption,
    isSubmitting,
    submitError,
    result,
    submit,
    reset,
  };
}
