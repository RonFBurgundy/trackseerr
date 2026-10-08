import React, { useEffect, useState } from 'react';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import {
  getUnseenChangelog,
  markChangelogSeen,
  type ChangelogRelease,
} from '@/services/changelogService';

export const WhatsNewModal: React.FC = () => {
  const [release, setRelease] = useState<ChangelogRelease | null>(null);
  const [isOpen, setIsOpen] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    getUnseenChangelog()
      .then((res) => {
        if (!cancelled && res.show && res.release) {
          setRelease(res.release);
          setIsOpen(true);
        }
      })
      .catch((err: unknown) => {
        console.error('Failed to check unseen changelog:', err);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const handleClose = () => {
    setIsOpen(false);
    markChangelogSeen().catch((err: unknown) => {
      console.error('Failed to mark changelog as seen:', err);
    });
  };

  if (!isOpen || !release) {
    return null;
  }

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={handleClose}
      title={`What's New in v${release.version}`}
      subtitle={release.date_note || undefined}
      maxWidth="sm:max-w-2xl"
      footer={
        <TapeDeckButton variant="amber" onClick={handleClose}>
          Got it
        </TapeDeckButton>
      }
    >
      <div className="space-y-4">
        {release.sections.map((section, idx) => (
          <div key={idx} className="space-y-1.5">
            <h4 className="text-xs font-mono font-bold uppercase tracking-wider text-[var(--accent-amber)]">
              {section.title}
            </h4>
            <ul className="space-y-1 pl-1">
              {section.items.map((item, itemIdx) => (
                <li
                  key={itemIdx}
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
    </ObsidianModal>
  );
};
