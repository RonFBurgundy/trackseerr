import { useState, useEffect, useCallback } from 'react';
import type { DiscoveryItem } from '@/types/models';
import { getTrending, getNewReleases, search as apiSearch } from '@/services/discoveryService';

export type DiscoveryCategory = 'trending' | 'new_releases' | 'search';

export interface UseDiscoveryReturn {
  category: DiscoveryCategory;
  query: string;
  items: DiscoveryItem[];
  isLoading: boolean;
  error: string | null;
  setCategory: (category: DiscoveryCategory) => void;
  search: (query: string) => Promise<void>;
  clearSearch: () => void;
  refresh: () => Promise<void>;
}

/** `enabled` must stay false until the user is signed in; the first load runs when it turns true. */
export function useDiscovery(enabled: boolean = true): UseDiscoveryReturn {
  const [category, setCategory] = useState<DiscoveryCategory>('trending');
  const [query, setQuery] = useState<string>('');
  const [items, setItems] = useState<DiscoveryItem[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const loadCategory = useCallback(async (cat: DiscoveryCategory, searchVal?: string) => {
    setIsLoading(true);
    setError(null);
    try {
      if (cat === 'trending') {
        const data = await getTrending();
        setItems(data);
      } else if (cat === 'new_releases') {
        const data = await getNewReleases();
        setItems(data);
      } else if (cat === 'search' && searchVal) {
        const data = await apiSearch(searchVal);
        setItems(data);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to load discovery items';
      setError(msg);
      setItems([]);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled && category !== 'search') {
      loadCategory(category);
    }
  }, [enabled, category, loadCategory]);

  const search = useCallback(async (searchQuery: string) => {
    setQuery(searchQuery);
    if (!searchQuery.trim()) {
      setCategory('trending');
      return;
    }
    setCategory('search');
    await loadCategory('search', searchQuery);
  }, [loadCategory]);

  const clearSearch = useCallback(() => {
    setQuery('');
    setCategory('trending');
  }, []);

  const refresh = useCallback(async () => {
    await loadCategory(category, query);
  }, [category, query, loadCategory]);

  return {
    category,
    query,
    items,
    isLoading,
    error,
    setCategory,
    search,
    clearSearch,
    refresh,
  };
}
