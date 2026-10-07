import type { NavSearchEntry } from './navSearchIndex';

const WORD_SPLIT = /[^a-z0-9]+/;

/** Score one query token against one lowercase text: exact word > word prefix > substring > in-order subsequence. */
export function tokenScore(token: string, text: string): number {
  if (token.length === 0 || text.length === 0) return 0;
  const words = text.split(WORD_SPLIT).filter((w) => w.length > 0);
  if (words.includes(token)) return words[0] === token ? 110 : 100;
  const prefixAt = words.findIndex((w) => w.startsWith(token));
  if (prefixAt >= 0) return prefixAt === 0 ? 90 : 80;
  if (text.includes(token)) return 50;
  if (token.length < 3) return 0;
  // Subsequence: every character in order, rewarding runs and word starts.
  let from = 0;
  let score = 10;
  let previous = -2;
  for (const ch of token) {
    const at = text.indexOf(ch, from);
    if (at < 0) return 0;
    if (at === previous + 1) score += 4;
    else if (at === 0 || !/[a-z0-9]/.test(text[at - 1])) score += 3;
    else score -= 1;
    previous = at;
    from = at + 1;
  }
  return Math.max(1, score);
}

/** Total score of `query` against an entry, or null when any token fails to match anywhere. */
export function scoreEntry(query: string, entry: NavSearchEntry): number | null {
  const tokens = query.toLowerCase().split(/\s+/).filter((t) => t.length > 0);
  if (tokens.length === 0) return null;
  const label = entry.label.toLowerCase();
  const path = entry.breadcrumb.join(' ').toLowerCase();
  const keywords = entry.keywords.join(' ').toLowerCase();
  let total = 0;
  for (const token of tokens) {
    const best = Math.max(tokenScore(token, label) * 3, tokenScore(token, path) * 1.5, tokenScore(token, keywords));
    if (best <= 0) return null;
    total += best;
  }
  // Prefer pages over deep settings on ties, then shorter labels.
  return total + (entry.target ? 0 : 5) - entry.label.length * 0.05;
}

/** Ranked matches, best first. */
export function searchNav(query: string, entries: readonly NavSearchEntry[], limit = 8): NavSearchEntry[] {
  return entries
    .map((entry) => ({ entry, score: scoreEntry(query, entry) }))
    .filter((r): r is { entry: NavSearchEntry; score: number } => r.score !== null)
    .sort((a, b) => b.score - a.score)
    .slice(0, limit)
    .map((r) => r.entry);
}
