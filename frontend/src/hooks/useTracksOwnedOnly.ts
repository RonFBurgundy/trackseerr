import { useCallback, useState } from 'react';

const OWNED_ONLY_KEY = 'trackseerr.library.tracksOwnedOnly';

function readOwnedOnly(): boolean {
  try {
    // Default on: only an explicit "0" turns it off.
    return localStorage.getItem(OWNED_ONLY_KEY) !== '0';
  } catch {
    return true;
  }
}

function writeOwnedOnly(value: boolean): void {
  try {
    localStorage.setItem(OWNED_ONLY_KEY, value ? '1' : '0');
  } catch {
    // Storage can be blocked (private window); the choice then lasts only for this session.
  }
}

export interface UseTracksOwnedOnlyReturn {
  ownedOnly: boolean;
  setOwnedOnly: (value: boolean) => void;
}

/** "Owned only" choice for the library Tracks tab, persisted per browser (default on). */
export function useTracksOwnedOnly(): UseTracksOwnedOnlyReturn {
  const [ownedOnly, setOwnedOnlyState] = useState<boolean>(readOwnedOnly);
  const setOwnedOnly = useCallback((value: boolean): void => {
    setOwnedOnlyState(value);
    writeOwnedOnly(value);
  }, []);
  return { ownedOnly, setOwnedOnly };
}
