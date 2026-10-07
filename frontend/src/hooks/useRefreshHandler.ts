import { createContext, useCallback, useContext, useEffect, useMemo, useRef } from 'react';

/** A view's "reload my data" function. May be sync or async. */
export type RefreshFn = () => void | Promise<unknown>;

export interface RefreshRegistry {
  /** Add a handler; returns the unregister function. */
  register: (fn: RefreshFn) => () => void;
  /** Run every registered handler. Resolves false (and runs nothing) when none is registered. */
  run: () => Promise<boolean>;
}

const noRegistry: RefreshRegistry = { register: () => () => undefined, run: () => Promise.resolve(false) };

export const RefreshContext = createContext<RefreshRegistry>(noRegistry);

/** Owns the set of handlers registered by the mounted views. Provide the result through `RefreshContext`. */
export function useRefreshRegistry(): RefreshRegistry {
  const handlers = useRef<Set<RefreshFn>>(new Set());
  const register = useCallback((fn: RefreshFn): (() => void) => {
    handlers.current.add(fn);
    return () => {
      handlers.current.delete(fn);
    };
  }, []);
  const run = useCallback(async (): Promise<boolean> => {
    const list = Array.from(handlers.current);
    if (list.length === 0) return false;
    // One failing handler must not hide the others; each hook already reports its own error.
    await Promise.allSettled(list.map((fn) => Promise.resolve().then(fn)));
    return true;
  }, []);
  return useMemo(() => ({ register, run }), [register, run]);
}

/** Register the current view's refresh function for pull-to-refresh while the component is mounted. */
export function useRefreshHandler(fn: RefreshFn): void {
  const { register } = useContext(RefreshContext);
  const latest = useRef<RefreshFn>(fn);
  latest.current = fn;
  useEffect(() => register(() => latest.current()), [register]);
}
