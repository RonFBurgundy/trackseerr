import React from 'react';
import { useRefreshHandler } from '@/hooks/useRefreshHandler';
import type { RefreshFn } from '@/hooks/useRefreshHandler';

/** Renders nothing; registers `onRefresh` for pull-to-refresh while mounted (lets a parent bind conditionally). */
export const RefreshBinding: React.FC<{ onRefresh: RefreshFn }> = ({ onRefresh }) => {
  useRefreshHandler(onRefresh);
  return null;
};
