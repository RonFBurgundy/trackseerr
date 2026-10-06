import type { LibraryHealthFinding, LibraryHealthGroup } from '@/types/libraryHealth';

/** Human labels for the known finding causes; order here is the display order. */
export const CAUSE_LABELS: Readonly<Record<string, string>> = {
  folder_not_in_server: 'Folder not in the server library',
  not_scanned_yet: 'Not scanned yet',
  unsupported_format: 'Unsupported format',
  corrupt: 'Corrupt or unplayable files',
  missing_tags: 'Missing tags',
  unreadable_permissions: 'Unreadable (permissions)',
  ignored_by_rule: 'Ignored by a server rule',
  unknown: 'Unknown cause',
  stale_entry: 'Stale server entries',
  weak_tag_match: 'Weak tag matches',
};

const CAUSE_ORDER: readonly string[] = Object.keys(CAUSE_LABELS);

export function causeLabel(cause: string): string {
  const known = CAUSE_LABELS[cause];
  if (known) return known;
  const words = cause.replace(/[_-]+/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'Other';
}

export interface CauseSection {
  cause: string;
  label: string;
  groups: LibraryHealthGroup[];
}

/** Buckets groups by cause in the canonical order; unknown causes follow alphabetically. */
export function sectionsByCause(groups: readonly LibraryHealthGroup[]): CauseSection[] {
  const byCause = new Map<string, LibraryHealthGroup[]>();
  for (const g of groups) byCause.set(g.cause, [...(byCause.get(g.cause) ?? []), g]);
  const rank = (cause: string): number => {
    const i = CAUSE_ORDER.indexOf(cause);
    return i === -1 ? CAUSE_ORDER.length : i;
  };
  return Array.from(byCause.entries())
    .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
    .map(([cause, list]) => ({ cause, label: causeLabel(cause), groups: list }));
}

/** Parent directory of a path (POSIX or Windows separators); the path itself when it has no separator. */
export function parentDir(path: string): string {
  const idx = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
  if (idx <= 0) return path;
  return path.slice(0, idx);
}

export function findingsOfGroup(findings: readonly LibraryHealthFinding[], group: LibraryHealthGroup): LibraryHealthFinding[] {
  return findings.filter((f) => f.group_key === group.group_key && f.kind === group.kind && f.cause === group.cause);
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

export interface WeakMatchInfo {
  title: string | null;
  sourceName: string | null;
  strength: string | null;
}

/** Narrows a weak_match finding's free-form `detail` to the fields the panel shows. */
export function weakMatchInfo(finding: LibraryHealthFinding): WeakMatchInfo {
  const d = finding.detail;
  return { title: str(d?.title), sourceName: str(d?.source_name), strength: str(d?.strength) };
}

export function fileName(path: string): string {
  const idx = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
  return idx === -1 ? path : path.slice(idx + 1);
}
