import React from 'react';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import type { ChangelogRelease } from '@/services/changelogService';

export interface ChangelogModalProps {
  isOpen: boolean;
  onClose: () => void;
  releases: ChangelogRelease[];
  currentVersion?: string | null;
  commit?: string | null;
}

export const ChangelogModal: React.FC<ChangelogModalProps> = ({
  isOpen,
  onClose,
  releases,
  currentVersion,
  commit,
}) => {
  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="Release History"
      subtitle={
        currentVersion
          ? `Current version: v${currentVersion}${commit ? ` (${commit})` : ''}`
          : undefined
      }
      maxWidth="sm:max-w-3xl"
      footer={
        <TapeDeckButton onClick={onClose}>
          Close
        </TapeDeckButton>
      }
    >
      <div className="space-y-6">
        {releases.length === 0 ? (
          <p className="text-xs font-mono text-neutral-400 py-4 text-center">
            No changelog entries available.
          </p>
        ) : (
          releases.map((rel) => {
            const isCurrent =
              currentVersion &&
              (rel.version === currentVersion ||
                rel.version.replace(/^v/, '') === currentVersion.replace(/^v/, ''));

            return (
              <div
                key={rel.version}
                className="border-b border-[#222222] pb-5 last:border-b-0 space-y-3"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-bold font-mono text-white">
                      {rel.unreleased ? 'Unreleased' : `v${rel.version}`}
                    </span>
                    {isCurrent && !rel.unreleased && (
                      <span className="px-1.5 py-0.5 rounded-[2px] bg-[var(--accent-amber)]/20 border border-[var(--accent-amber)]/40 text-[10px] font-mono text-[var(--accent-amber)] font-semibold">
                        CURRENT
                      </span>
                    )}
                    {rel.unreleased && (
                      <span className="px-1.5 py-0.5 rounded-[2px] bg-neutral-800 border border-neutral-700 text-[10px] font-mono text-neutral-400">
                        DEVELOPMENT
                      </span>
                    )}
                  </div>
                  {rel.date_note && (
                    <span className="text-xs font-mono text-neutral-400">
                      {rel.date_note}
                    </span>
                  )}
                </div>

                {rel.sections.length === 0 ? (
                  <p className="text-xs font-mono text-neutral-500 italic pl-1">
                    No release notes recorded.
                  </p>
                ) : (
                  <div className="space-y-3 pl-1">
                    {rel.sections.map((sec, sIdx) => (
                      <div key={sIdx} className="space-y-1.5">
                        <h5 className="text-[11px] font-mono font-bold uppercase tracking-wider text-[var(--accent-amber)]">
                          {sec.title}
                        </h5>
                        <ul className="space-y-1 pl-1">
                          {sec.items.map((item, iIdx) => (
                            <li
                              key={iIdx}
                              className="flex items-start gap-2 text-xs font-mono text-neutral-300 leading-relaxed"
                            >
                              <span className="text-neutral-500 select-none mt-0.5">&bull;</span>
                              <span>{item}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </ObsidianModal>
  );
};
