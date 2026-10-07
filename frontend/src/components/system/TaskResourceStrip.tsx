import React from 'react';
import { useSystemResources } from '@/hooks/useSystemResources';
import { formatRss, formatCount, formatPercent, formatUptime } from './taskFormat';

interface GaugeProps {
  label: string;
  value: string;
}

const Gauge: React.FC<GaugeProps> = ({ label, value }) => (
  <div className="min-w-0 px-3 py-2 bg-[#0d0d0d] border border-[#1f1f1f] rounded-[3px] shadow-[inset_0_2px_4px_rgba(0,0,0,0.8)]">
    <dt className="text-[10px] font-mono uppercase tracking-wider text-neutral-500">{label}</dt>
    <dd className="text-sm font-mono font-bold text-white truncate">{value}</dd>
  </div>
);

/** Process resources gauge strip: CPU, RSS, threads, uptime. Unknown values read as an em dash. */
export const TaskResourceStrip: React.FC = React.memo(() => {
  const { resources, error } = useSystemResources();
  return (
    <section aria-label="Process resources">
      <dl className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <Gauge label="CPU" value={formatPercent(resources?.cpu_percent)} />
        <Gauge label="Memory (RSS)" value={formatRss(resources?.rss_bytes)} />
        <Gauge label="Threads" value={formatCount(resources?.thread_count)} />
        <Gauge label="Uptime" value={formatUptime(resources?.uptime_seconds)} />
      </dl>
      {error && resources === null && (
        <p className="mt-1 text-[11px] font-mono text-neutral-500" role="status">
          {error}
        </p>
      )}
    </section>
  );
});
TaskResourceStrip.displayName = 'TaskResourceStrip';
