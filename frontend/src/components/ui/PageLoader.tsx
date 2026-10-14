import React from 'react';
import { CassetteLoader } from './CassetteLoader';

export interface PageLoaderProps {
  size?: 'sm' | 'md';
  message?: string;
  skeletonRows?: number;
  className?: string;
}

export const PageLoader: React.FC<PageLoaderProps> = ({
  size = 'md',
  message,
  skeletonRows,
  className = '',
}) => {
  const padClass = size === 'sm' ? 'py-6' : 'py-10';

  if (!skeletonRows || skeletonRows <= 0) {
    return (
      <div className={`flex items-center justify-center ${padClass} ${className}`.trim()}>
        <CassetteLoader size={size} message={message} />
      </div>
    );
  }

  const rows = Array.from({ length: skeletonRows }, (_, i) => i);

  return (
    <div className={`relative p-3 space-y-2 ${className}`.trim()}>
      <div aria-hidden="true" className="space-y-2">
        {rows.map((i) => (
          <div
            key={i}
            className="h-10 rounded-[3px] bg-[#1a1a1a] animate-pulse motion-reduce:animate-none"
          />
        ))}
      </div>
      <div className="absolute inset-0 flex items-center justify-center p-3 pointer-events-none">
        <div className="bg-[#0a0a0a]/70 rounded-[4px] p-3 pointer-events-auto">
          <CassetteLoader size={size} message={message} />
        </div>
      </div>
    </div>
  );
};
