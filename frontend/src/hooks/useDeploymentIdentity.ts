import { useEffect, useState } from 'react';
import type { DeploymentTier } from '@/types/models';
import { getPublicTier } from '@/services/deploymentService';

export const GATEWAY_BRAND = 'TrackSeerr Requests';
const DEFAULT_TITLE = 'TrackSeerr';

export interface DeploymentIdentity {
  tier: DeploymentTier;
  isGateway: boolean;
  isCore: boolean;
  /** Plain-text brand, used for document.title and aria labels. */
  brandName: string;
}

/**
 * Resolves the tier for branding: the authenticated tier wins; before sign-in the
 * public /api/health probe is used. Also keeps document.title in sync.
 */
export function useDeploymentIdentity(
  authTier: DeploymentTier,
  isAuthenticated: boolean
): DeploymentIdentity {
  const [publicTier, setPublicTier] = useState<DeploymentTier | null>(null);

  useEffect(() => {
    if (isAuthenticated) return;
    let cancelled = false;
    void getPublicTier().then((t) => {
      if (!cancelled) setPublicTier(t);
    });
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated]);

  const tier: DeploymentTier = isAuthenticated ? authTier : (publicTier ?? 'all-in-one');
  const isGateway = tier === 'gateway';
  const brandName = isGateway ? GATEWAY_BRAND : DEFAULT_TITLE;

  useEffect(() => {
    document.title = brandName;
  }, [brandName]);

  return { tier, isGateway, isCore: tier === 'core', brandName };
}
