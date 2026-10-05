import { useCallback, useEffect, useMemo, useState } from 'react';
import type { QualityDefinition } from '@/types/qualityDefinitions';
import {
  DEFAULT_MIN_FORMAT_SCORE,
  type FormatScore,
  type QualityEntry,
  type QualityProfile,
  type QualityProfileInput,
} from '@/types/qualityProfiles';

export const entryLabel = (e: QualityEntry): string => (e.type === 'group' ? e.name : e.quality);
export const entryQualities = (e: QualityEntry): string[] => (e.type === 'group' ? e.items : [e.quality]);

const INTEGER = /^-?\d+$/;

/** Appends qualities the profile does not mention yet as disallowed entries, so every quality is always listed. */
function withAllQualities(entries: QualityEntry[], all: readonly string[]): QualityEntry[] {
  const present = new Set(entries.flatMap(entryQualities));
  const missing = all.filter((q) => !present.has(q));
  return [...entries, ...missing.map((quality): QualityEntry => ({ type: 'quality', quality, allowed: false }))];
}

/** A stored cutoff may name a quality inside a group; the editor selects the group instead. */
function cutoffLabel(entries: QualityEntry[], cutoff: string): string {
  const hit = entries.find((e) => entryLabel(e) === cutoff || entryQualities(e).includes(cutoff));
  return hit ? entryLabel(hit) : cutoff;
}

function parseInteger(text: string): number | null {
  return INTEGER.test(text.trim()) ? Number(text.trim()) : null;
}

export interface UseQualityProfileDraftReturn {
  name: string;
  setName: (v: string) => void;
  upgradeAllowed: boolean;
  setUpgradeAllowed: (v: boolean) => void;
  entries: QualityEntry[];
  reorder: (next: QualityEntry[]) => void;
  toggleAllowed: (index: number) => void;
  renameGroup: (index: number, name: string) => void;
  selected: readonly string[];
  toggleSelected: (quality: string) => void;
  /** Groups the selected qualities; resolves an error message or null. */
  createGroup: (name: string) => string | null;
  ungroup: (index: number) => void;
  cutoff: string;
  setCutoff: (v: string) => void;
  /** Allowed entry labels, plus the stored cutoff when it names a disallowed entry. */
  cutoffOptions: string[];
  /** True when the selected cutoff names an entry that is not allowed (the backend permits it; the editor warns). */
  cutoffNotAllowed: boolean;
  scores: Readonly<Record<number, string>>;
  setScore: (formatId: number, value: string) => void;
  minScore: string;
  setMinScore: (v: string) => void;
  cutoffScore: string;
  setCutoffScore: (v: string) => void;
  minUpgrade: string;
  setMinUpgrade: (v: string) => void;
  /** First problem blocking a save, or null. */
  problem: string | null;
  toInput: () => QualityProfileInput | null;
}

/** Editable copy of one quality profile (or a blank one). All editor state lives here; the modal stays declarative. */
export function useQualityProfileDraft(profile: QualityProfile | null, definitions: readonly QualityDefinition[]): UseQualityProfileDraftReturn {
  const [name, setName] = useState<string>(profile?.name ?? '');
  const [upgradeAllowed, setUpgradeAllowed] = useState<boolean>(profile?.upgrade_allowed ?? true);
  const [entries, setEntries] = useState<QualityEntry[]>(() =>
    withAllQualities(profile?.items ?? [], definitions.map((d) => d.quality))
  );
  const [selected, setSelected] = useState<string[]>([]);
  const [cutoffRaw, setCutoffRaw] = useState<string>(() =>
    profile ? cutoffLabel(withAllQualities(profile.items, definitions.map((d) => d.quality)), profile.cutoff) : ''
  );
  const [scores, setScores] = useState<Record<number, string>>(() => {
    const init: Record<number, string> = {};
    for (const fi of profile?.format_items ?? []) init[fi.format_id] = String(fi.score);
    return init;
  });
  const [minScore, setMinScore] = useState<string>(String(profile?.min_format_score ?? DEFAULT_MIN_FORMAT_SCORE));
  const [cutoffScore, setCutoffScore] = useState<string>(String(profile?.cutoff_format_score ?? 0));
  const [minUpgrade, setMinUpgrade] = useState<string>(String(profile?.min_upgrade_format_score ?? 1));

  // Definitions can arrive after the modal opens: append any quality not yet listed, as disallowed, keeping order and edits.
  useEffect(() => {
    const all = definitions.map((d) => d.quality);
    setEntries((prev) => {
      const present = new Set(prev.flatMap(entryQualities));
      return all.some((q) => !present.has(q)) ? withAllQualities(prev, all) : prev;
    });
  }, [definitions]);

  const allowedLabels = useMemo(() => entries.filter((e) => e.allowed).map(entryLabel), [entries]);
  const knownLabels = useMemo(() => entries.map(entryLabel), [entries]);
  // The stored cutoff is kept even when disallowed; it only falls back when blank or no longer an entry at all.
  const cutoff = cutoffRaw !== '' && knownLabels.includes(cutoffRaw) ? cutoffRaw : (allowedLabels[0] ?? '');
  const cutoffNotAllowed = cutoff !== '' && !allowedLabels.includes(cutoff);
  const cutoffOptions = useMemo(() => (cutoffNotAllowed ? [...allowedLabels, cutoff] : allowedLabels), [allowedLabels, cutoffNotAllowed, cutoff]);

  const reorder = useCallback((next: QualityEntry[]): void => setEntries(next), []);

  const toggleAllowed = useCallback((index: number): void => {
    setEntries((prev) => prev.map((e, i) => (i === index ? { ...e, allowed: !e.allowed } : e)));
  }, []);

  const renameGroup = useCallback(
    (index: number, nextName: string): void => {
      const target = entries[index];
      if (!target || target.type !== 'group') return;
      if (cutoffRaw === target.name) setCutoffRaw(nextName);
      setEntries((prev) => prev.map((e, i) => (i === index && e.type === 'group' ? { ...e, name: nextName } : e)));
    },
    [entries, cutoffRaw]
  );

  const toggleSelected = useCallback((quality: string): void => {
    setSelected((prev) => (prev.includes(quality) ? prev.filter((q) => q !== quality) : [...prev, quality]));
  }, []);

  const createGroup = useCallback(
    (rawName: string): string | null => {
      const groupName = rawName.trim();
      const members = entries.flatMap((e) => (e.type === 'quality' && selected.includes(e.quality) ? [e.quality] : []));
      if (members.length < 2) return 'Select at least two qualities to group.';
      if (!groupName) return 'Name the group.';
      if (entries.some((e) => entryLabel(e).toLowerCase() === groupName.toLowerCase())) return `"${groupName}" is already used.`;
      const firstIndex = entries.findIndex((e) => e.type === 'quality' && selected.includes(e.quality));
      const group: QualityEntry = {
        type: 'group',
        name: groupName,
        allowed: entries.some((e) => e.type === 'quality' && selected.includes(e.quality) && e.allowed),
        items: members,
      };
      const rest = entries.filter((e) => !(e.type === 'quality' && selected.includes(e.quality)));
      const insertAt = entries.slice(0, firstIndex).filter((e) => !(e.type === 'quality' && selected.includes(e.quality))).length;
      setEntries([...rest.slice(0, insertAt), group, ...rest.slice(insertAt)]);
      setSelected([]);
      return null;
    },
    [entries, selected]
  );

  const ungroup = useCallback((index: number): void => {
    setEntries((prev) => {
      const g = prev[index];
      if (!g || g.type !== 'group') return prev;
      const members = g.items.map((quality): QualityEntry => ({ type: 'quality', quality, allowed: g.allowed }));
      return [...prev.slice(0, index), ...members, ...prev.slice(index + 1)];
    });
  }, []);

  const setScore = useCallback((formatId: number, value: string): void => {
    setScores((prev) => ({ ...prev, [formatId]: value }));
  }, []);

  const validated = useMemo((): { input: Omit<QualityProfileInput, 'id' | 'is_default'> | null; problem: string | null } => {
    if (!name.trim()) return { input: null, problem: 'Give the profile a name.' };
    if (allowedLabels.length === 0) return { input: null, problem: 'Allow at least one quality.' };
    const min = parseInteger(minScore);
    const upto = parseInteger(cutoffScore);
    const inc = parseInteger(minUpgrade);
    if (min === null) return { input: null, problem: 'Minimum format score must be a whole number.' };
    if (upto === null) return { input: null, problem: 'Upgrade-until score must be a whole number.' };
    if (inc === null || inc < 1) return { input: null, problem: 'Minimum upgrade increment must be a whole number of at least 1.' };
    const formatItems: FormatScore[] = [];
    for (const [id, text] of Object.entries(scores)) {
      if (text.trim() === '') continue;
      const score = parseInteger(text);
      if (score === null) return { input: null, problem: 'Format scores must be whole numbers.' };
      if (score !== 0) formatItems.push({ format_id: Number(id), score });
    }
    return {
      problem: null,
      input: {
        name: name.trim(),
        cutoff,
        items: entries,
        upgrade_allowed: upgradeAllowed,
        min_format_score: min,
        cutoff_format_score: upto,
        min_upgrade_format_score: inc,
        format_items: formatItems,
      },
    };
  }, [name, allowedLabels, minScore, cutoffScore, minUpgrade, scores, cutoff, entries, upgradeAllowed]);

  const toInput = useCallback((): QualityProfileInput | null => {
    if (!validated.input) return null;
    return { ...validated.input, ...(profile ? { id: profile.id } : {}), is_default: profile?.is_default ?? false };
  }, [validated, profile]);

  return {
    name,
    setName,
    upgradeAllowed,
    setUpgradeAllowed,
    entries,
    reorder,
    toggleAllowed,
    renameGroup,
    selected,
    toggleSelected,
    createGroup,
    ungroup,
    cutoff,
    setCutoff: setCutoffRaw,
    cutoffOptions,
    cutoffNotAllowed,
    scores,
    setScore,
    minScore,
    setMinScore,
    cutoffScore,
    setCutoffScore,
    minUpgrade,
    setMinUpgrade,
    problem: validated.problem,
    toInput,
  };
}
