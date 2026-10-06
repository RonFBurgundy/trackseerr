import React, { useState } from 'react';
import { ChevronDown, EyeOff, FolderX } from 'lucide-react';
import { MachinedCard, TapeDeckButton } from '@/components/ui';
import type { LibraryHealthFinding, LibraryHealthGroup } from '@/types/libraryHealth';
import { findingsOfGroup, parentDir, sectionsByCause } from './reviewCauses';

export interface ReviewGroupListProps {
  groups: readonly LibraryHealthGroup[];
  findings: readonly LibraryHealthFinding[];
  busy: boolean;
  onDismiss: (path: string, scope: 'file' | 'folder') => Promise<void>;
}

const groupId = (g: LibraryHealthGroup): string => `${g.kind}|${g.cause}|${g.group_key}`;

/** Groups bucketed under a heading per cause; each row expands to its findings with dismiss actions. */
export const ReviewGroupList: React.FC<ReviewGroupListProps> = React.memo(({ groups, findings, busy, onDismiss }) => {
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set());

  const toggle = (id: string): void =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="space-y-4">
      {sectionsByCause(groups).map((section) => (
        <section key={section.cause} aria-label={section.label} className="space-y-2">
          <h5 className="text-[11px] font-bold uppercase tracking-widest font-mono text-[var(--text-secondary)]">
            {section.label}
            <span className="ml-2 font-normal text-[var(--text-muted)]">
              {section.groups.reduce((n, g) => n + g.count, 0)}
            </span>
          </h5>
          {section.groups.map((g) => {
            const id = groupId(g);
            const isOpen = open.has(id);
            const items = isOpen ? findingsOfGroup(findings, g) : [];
            const folder = g.group_key || parentDir(g.sample_path);
            return (
              <MachinedCard key={id} className="p-2.5 space-y-2">
                <div className="flex flex-col sm:flex-row sm:items-center gap-2">
                  <button
                    type="button"
                    aria-expanded={isOpen}
                    onClick={() => toggle(id)}
                    className="flex flex-1 min-w-0 items-start gap-2 text-left"
                  >
                    <ChevronDown
                      className={`mt-0.5 h-4 w-4 shrink-0 text-[var(--text-muted)] transition-transform ${isOpen ? 'rotate-180' : ''}`}
                      aria-hidden="true"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2">
                        <span className="text-xs font-mono font-bold text-[var(--accent-amber)]">{g.count}</span>
                        <span className="block truncate text-xs font-mono text-[var(--text-primary)]" title={g.sample_path}>
                          {g.sample_path}
                        </span>
                      </span>
                      {g.suggestion && (
                        <span className="mt-0.5 block text-[11px] text-[var(--text-secondary)]">{g.suggestion}</span>
                      )}
                    </span>
                  </button>
                  <TapeDeckButton
                    size="sm"
                    disabled={busy}
                    onClick={() => void onDismiss(folder, 'folder')}
                    icon={<FolderX className="h-3.5 w-3.5" />}
                    title={`Dismiss everything under ${folder}`}
                  >
                    Dismiss folder
                  </TapeDeckButton>
                </div>
                {isOpen && (
                  <ul className="border-t border-[var(--border-subtle)] pt-2 space-y-1">
                    {items.length === 0 && <li className="text-[11px] font-mono text-[var(--text-muted)]">No file details available</li>}
                    {items.map((f) => (
                      <li key={f.id} className="flex items-center gap-2">
                        <span className="min-w-0 flex-1 truncate text-[11px] font-mono text-[var(--text-secondary)]" title={f.path}>
                          {f.path}
                        </span>
                        <TapeDeckButton
                          size="sm"
                          disabled={busy}
                          onClick={() => void onDismiss(f.path, 'file')}
                          icon={<EyeOff className="h-3.5 w-3.5" />}
                          aria-label={`Dismiss file ${f.path}`}
                        >
                          Dismiss file
                        </TapeDeckButton>
                      </li>
                    ))}
                  </ul>
                )}
              </MachinedCard>
            );
          })}
        </section>
      ))}
    </div>
  );
});
ReviewGroupList.displayName = 'ReviewGroupList';
