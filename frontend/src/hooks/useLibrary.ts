import { useState, useEffect, useCallback, useRef } from 'react';
import type {
  CollectionItem,
  LibraryStats,
  ScanStatus,
  LidarrStatus,
} from '@/types/models';
import {
  getCollections,
  getLibraryStats,
  triggerScan as apiTriggerScan,
  getScanStatus as apiGetScanStatus,
  cancelScan as apiCancelScan,
  getLidarrStatus as apiGetLidarrStatus,
  cancelLidarrMigration as apiCancelLidarrMigration,
  toggleArtistMonitored as apiToggleArtistMonitored,
  toggleAlbumMonitored as apiToggleAlbumMonitored,
  toggleTrackMonitored as apiToggleTrackMonitored,
} from '@/services/libraryService';

export type LibraryTab = 'artists' | 'albums' | 'tracks' | 'collections';

export interface UseLibraryReturn {
  activeTab: LibraryTab;
  collections: CollectionItem[];
  stats: LibraryStats | null;
  searchQuery: string;
  isScanning: boolean;
  scanStatus: ScanStatus | null;
  lidarrStatus: LidarrStatus | null;
  isLoading: boolean;
  error: string | null;
  /** Bumps when a scan finishes or is cancelled; paged lists reload on change. */
  catalogVersion: number;
  setTab: (tab: LibraryTab) => void;
  setSearch: (query: string) => void;
  triggerScan: (pruneMissing?: boolean) => Promise<void>;
  cancelScan: () => Promise<void>;
  cancelLidarrImport: () => Promise<void>;
  toggleArtistMonitored: (artistId: number | string, monitored: boolean) => Promise<void>;
  toggleAlbumMonitored: (albumId: number | string, monitored: boolean) => Promise<void>;
  toggleTrackMonitored: (trackId: number | string, monitored: boolean) => Promise<void>;
  refresh: () => Promise<void>;
  /** Reloads stats/collections and remounts the paged catalog lists (after files were imported). */
  reloadCatalog: () => Promise<void>;
}

/** `enabled` must be false for non-admins: every /api/library route except availability is admin-only. */
export function useLibrary(enabled: boolean = false): UseLibraryReturn {
  const [activeTab, setActiveTab] = useState<LibraryTab>('artists');
  const [collections, setCollections] = useState<CollectionItem[]>([]);
  const [stats, setStats] = useState<LibraryStats | null>(null);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [isScanning, setIsScanning] = useState<boolean>(false);
  const [scanStatus, setScanStatus] = useState<ScanStatus | null>(null);
  const [lidarrStatus, setLidarrStatus] = useState<LidarrStatus | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [catalogVersion, setCatalogVersion] = useState<number>(0);

  const scanPollRef = useRef<number | null>(null);
  const lidarrPollRef = useRef<number | null>(null);

  const stopScanPolling = useCallback(() => {
    if (scanPollRef.current !== null) {
      window.clearInterval(scanPollRef.current);
      scanPollRef.current = null;
    }
  }, []);

  const stopLidarrPolling = useCallback(() => {
    if (lidarrPollRef.current !== null) {
      window.clearInterval(lidarrPollRef.current);
      lidarrPollRef.current = null;
    }
  }, []);

  const collectionQuery = activeTab === 'collections' ? searchQuery : '';

  const loadData = useCallback(async () => {
    if (!enabled) return;
    setIsLoading(true);
    setError(null);
    try {
      const [statsData, lidarrData] = await Promise.all([
        getLibraryStats().catch(() => null),
        apiGetLidarrStatus().catch(() => null),
      ]);
      if (statsData) setStats(statsData);
      if (lidarrData) setLidarrStatus(lidarrData);

      // Artists, albums and tracks are paged by their own lists (useLibraryCatalog); only collections load here.
      if (activeTab === 'collections') {
        const data = await getCollections(collectionQuery);
        setCollections(data);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to load library items';
      setError(msg);
    } finally {
      setIsLoading(false);
    }
    // The search text only feeds collections here; the paged lists take it as a server filter.
  }, [enabled, activeTab, collectionQuery]);

  const loadDataRef = useRef(loadData);
  useEffect(() => {
    loadDataRef.current = loadData;
  }, [loadData]);

  const reloadCatalog = useCallback(async () => {
    setCatalogVersion((v) => v + 1);
    await loadData();
  }, [loadData]);

  const reloadCatalogRef = useRef(reloadCatalog);
  useEffect(() => {
    reloadCatalogRef.current = reloadCatalog;
  }, [reloadCatalog]);

  const startLidarrPolling = useCallback(() => {
    stopLidarrPolling();
    lidarrPollRef.current = window.setInterval(async () => {
      try {
        const nextStatus = await apiGetLidarrStatus();
        setLidarrStatus(nextStatus);
        if (!nextStatus.is_migrating) {
          stopLidarrPolling();
          await reloadCatalogRef.current();
          const statsData = await getLibraryStats().catch(() => null);
          if (statsData) setStats(statsData);
        }
      } catch {
        stopLidarrPolling();
      }
    }, 3000);
  }, [stopLidarrPolling]);

  useEffect(() => {
    if (lidarrStatus?.is_migrating) {
      if (lidarrPollRef.current === null) {
        startLidarrPolling();
      }
    } else {
      stopLidarrPolling();
    }
  }, [lidarrStatus?.is_migrating, startLidarrPolling, stopLidarrPolling]);

  useEffect(() => {
    return () => {
      stopLidarrPolling();
    };
  }, [stopLidarrPolling]);

  const startScanPolling = useCallback(() => {
    stopScanPolling();
    scanPollRef.current = window.setInterval(async () => {
      try {
        const [status, statsData] = await Promise.all([
          apiGetScanStatus(),
          getLibraryStats().catch(() => null),
        ]);
        setScanStatus(status);
        if (statsData) {
          setStats(statsData);
        }

        const isCurrentlyScanning = Boolean(
          status.is_scanning || status.status === 'scanning' || status.status === 'running'
        );
        if (!isCurrentlyScanning) {
          setIsScanning(false);
          stopScanPolling();
          setCatalogVersion((v) => v + 1);
          loadDataRef.current();
        }
      } catch {
        setIsScanning(false);
        stopScanPolling();
      }
    }, 1500);
  }, [stopScanPolling]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // Check if a scan is already running on mount
  useEffect(() => {
    if (!enabled) return undefined;
    let isCancelled = false;
    const checkInitialScan = async () => {
      try {
        const status = await apiGetScanStatus();
        if (
          !isCancelled &&
          (status.is_scanning || status.status === 'scanning' || status.status === 'running')
        ) {
          setIsScanning(true);
          setScanStatus(status);
          startScanPolling();
        }
      } catch {
        // ignore error fetching initial scan status
      }
    };
    checkInitialScan();
    return () => {
      isCancelled = true;
    };
  }, [enabled, startScanPolling]);

  useEffect(() => {
    return () => {
      stopScanPolling();
    };
  }, [stopScanPolling]);

  const triggerScan = useCallback(
    async (pruneMissing: boolean = false) => {
      setError(null);
      setIsScanning(true);
      try {
        await apiTriggerScan(pruneMissing);
        startScanPolling();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Failed to trigger library scan';
        setError(msg);
        setIsScanning(false);
      }
    },
    [startScanPolling]
  );

  const cancelScan = useCallback(async () => {
    try {
      await apiCancelScan();
    } finally {
      setIsScanning(false);
      stopScanPolling();
      setCatalogVersion((v) => v + 1);
      await loadData();
    }
  }, [loadData, stopScanPolling]);

  const cancelLidarrImport = useCallback(async () => {
    try {
      const res = await apiCancelLidarrMigration();
      setLidarrStatus(res);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to cancel Lidarr import';
      setError(msg);
    }
  }, []);

  const toggleArtistMonitored = useCallback(
    async (artistId: number | string, monitored: boolean) => {
      await apiToggleArtistMonitored(artistId, monitored);
    },
    []
  );

  const toggleAlbumMonitored = useCallback(
    async (albumId: number | string, monitored: boolean) => {
      await apiToggleAlbumMonitored(albumId, monitored);
    },
    []
  );

  const toggleTrackMonitored = useCallback(
    async (trackId: number | string, monitored: boolean) => {
      await apiToggleTrackMonitored(trackId, monitored);
    },
    []
  );

  return {
    activeTab,
    collections,
    stats,
    searchQuery,
    isScanning,
    scanStatus,
    lidarrStatus,
    isLoading,
    error,
    catalogVersion,
    setTab: setActiveTab,
    setSearch: setSearchQuery,
    triggerScan,
    cancelScan,
    cancelLidarrImport,
    toggleArtistMonitored,
    toggleAlbumMonitored,
    toggleTrackMonitored,
    refresh: loadData,
    reloadCatalog,
  };
}
