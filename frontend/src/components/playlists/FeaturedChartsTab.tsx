import React, { useMemo, useState } from 'react';
import { Loader2 } from 'lucide-react';
import type { FeaturedChart } from '@/types/models';
import { useFeaturedCharts } from '@/hooks/useFeaturedCharts';
import { TapeDeckButton } from '@/components/ui';
import { errorMessage } from '@/services/apiClient';

export interface FeaturedChartsTabProps {
  enabled: boolean;
  onSubscribe: (chart: FeaturedChart) => Promise<void>;
}

export const FeaturedChartsTab: React.FC<FeaturedChartsTabProps> = ({ enabled, onSubscribe }) => {
  const { charts, isLoading, error: loadError } = useFeaturedCharts(enabled);
  const [subscribingId, setSubscribingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const groupedCharts = useMemo(() => {
    const groups = new Map<string, FeaturedChart[]>();
    for (const chart of charts) {
      const category = chart.category || 'Featured';
      const existing = groups.get(category);
      if (existing) {
        existing.push(chart);
      } else {
        groups.set(category, [chart]);
      }
    }
    return groups;
  }, [charts]);

  const handleSubscribe = async (chart: FeaturedChart) => {
    setSubscribingId(chart.id);
    setActionError(null);
    try {
      await onSubscribe(chart);
    } catch (err: unknown) {
      setActionError(errorMessage(err, 'Failed to add chart'));
    } finally {
      setSubscribingId(null);
    }
  };

  if (isLoading && charts.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-8 text-neutral-400">
        <Loader2 className="h-6 w-6 animate-spin text-[#e5a00d] mb-2" />
        <span className="text-xs font-mono">Loading featured charts...</span>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {loadError && (
        <div
          role="alert"
          className="p-2.5 bg-red-950/40 border border-red-800/50 rounded-[3px] text-xs font-mono text-red-300"
        >
          {loadError}
        </div>
      )}

      {actionError && (
        <div
          role="alert"
          className="p-2.5 bg-red-950/40 border border-red-800/50 rounded-[3px] text-xs font-mono text-red-300"
        >
          {actionError}
        </div>
      )}

      {charts.length === 0 && !isLoading && !loadError ? (
        <div className="p-6 text-center text-xs text-neutral-500 font-mono bg-[#141414] border border-[#222222] rounded-[4px]">
          No featured charts available.
        </div>
      ) : (
        Array.from(groupedCharts.entries()).map(([category, items]) => (
          <div key={category} className="space-y-2">
            <h4 className="text-xs font-mono uppercase tracking-wider text-[#e5a00d] font-semibold">
              {category}
            </h4>
            <div className="flex flex-col gap-2">
              {items.map((chart) => {
                const isSubscribing = subscribingId === chart.id;
                return (
                  <div
                    key={chart.id}
                    className="flex items-center gap-3 p-2.5 bg-[#141414] border border-[#222222] rounded-[4px] hover:border-[#2f2f2f] transition-colors"
                  >
                    <img
                      src={chart.poster_url}
                      alt={chart.name}
                      width={48}
                      height={48}
                      loading="lazy"
                      decoding="async"
                      className="w-12 h-12 shrink-0 rounded-[3px] object-cover bg-[#0d0d0d] border border-[#222222]"
                    />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-0.5">
                        <span className="font-semibold text-sm text-white truncate" title={chart.name}>
                          {chart.name}
                        </span>
                        <span className="shrink-0 px-1.5 py-0.5 text-[10px] uppercase font-mono tracking-wider bg-[#1f1f1f] border border-[#2a2a2a] text-neutral-300 rounded-[3px]">
                          {chart.service}
                        </span>
                      </div>
                      <p className="line-clamp-2 text-xs text-neutral-400 font-mono" title={chart.description}>
                        {chart.description}
                      </p>
                    </div>
                    <div className="shrink-0">
                      <TapeDeckButton
                        size="sm"
                        variant="amber"
                        disabled={isSubscribing}
                        onClick={() => void handleSubscribe(chart)}
                        icon={isSubscribing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
                      >
                        Add
                      </TapeDeckButton>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))
      )}
    </div>
  );
};
