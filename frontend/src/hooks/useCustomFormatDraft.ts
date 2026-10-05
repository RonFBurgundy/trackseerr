import { useCallback, useMemo, useRef, useState } from 'react';
import {
  SPEC_SOURCES,
  SPEC_TYPES,
  type CustomFormat,
  type CustomFormatInput,
  type CustomFormatSpec,
  type SpecFieldValue,
} from '@/types/customFormats';
import { RELEASE_PROTOCOLS } from '@/types/qualityProfiles';

export interface SpecDraft {
  key: number;
  name: string;
  implementation: string;
  negate: boolean;
  required: boolean;
  /** Editable text for the supported types; ignored for unsupported specs. */
  fields: Record<string, string>;
  /** Original fields of an unsupported spec, sent back untouched so exports round-trip. */
  raw: Record<string, SpecFieldValue>;
  supported: boolean;
}

const isSupportedImplementation = (impl: string): boolean => SPEC_TYPES.some((t) => t.implementation === impl);

const fieldText = (v: SpecFieldValue | undefined): string => (v === null || v === undefined ? '' : String(v));

/** Starting field values for a freshly chosen spec type. */
export function defaultFields(implementation: string, firstQuality: string): Record<string, string> {
  switch (implementation) {
    case 'SizeSpecification':
      return { min: '0', max: '' };
    case 'PhraseSpecification':
      return { value: '', threshold: '85' };
    case 'QualitySpecification':
      return { value: firstQuality };
    case 'SourceSpecification':
      return { value: SPEC_SOURCES[0] };
    case 'ProtocolSpecification':
      return { value: RELEASE_PROTOCOLS[0] };
    default:
      return { value: '' };
  }
}

function fromSpec(spec: CustomFormatSpec, key: number): SpecDraft {
  const supported = isSupportedImplementation(spec.implementation);
  const fields: Record<string, string> = {};
  for (const [k, v] of Object.entries(spec.fields)) fields[k] = fieldText(v);
  return { key, name: spec.name, implementation: spec.implementation, negate: spec.negate, required: spec.required, fields, raw: spec.fields, supported };
}

type Built = { spec: CustomFormatInput['specifications'][number] } | { problem: string };

function build(d: SpecDraft, label: string): Built {
  const base = { name: d.name.trim() || d.implementation, implementation: d.implementation, negate: d.negate, required: d.required };
  if (!d.supported) return { spec: { ...base, fields: d.raw } };
  const f = d.fields;
  const num = (text: string | undefined): number | null => (text !== undefined && text.trim() !== '' && Number.isFinite(Number(text)) ? Number(text) : null);
  switch (d.implementation) {
    case 'SizeSpecification': {
      const min = f.min?.trim() === '' || f.min === undefined ? 0 : num(f.min);
      const max = f.max?.trim() === '' || f.max === undefined ? 0 : num(f.max);
      if (min === null || max === null || min < 0 || max < 0) return { problem: `${label}: size bounds must be numbers of GB, 0 or more.` };
      if (max > 0 && max <= min) return { problem: `${label}: max must be greater than min.` };
      return { spec: { ...base, fields: { min, max } } };
    }
    case 'IndexerFlagSpecification': {
      const v = num(f.value);
      if (v === null || !Number.isInteger(v)) return { problem: `${label}: flag must be a whole number.` };
      return { spec: { ...base, fields: { value: v } } };
    }
    case 'PhraseSpecification': {
      const t = num(f.threshold);
      if (!f.value?.trim()) return { problem: `${label}: enter a phrase.` };
      if (t === null || t < 1 || t > 100) return { problem: `${label}: threshold must be between 1 and 100.` };
      return { spec: { ...base, fields: { value: f.value.trim(), threshold: t } } };
    }
    case 'ReleaseTitleSpecification':
    case 'ReleaseGroupSpecification':
      if (!f.value?.trim()) return { problem: `${label}: enter a regular expression.` };
      return { spec: { ...base, fields: { value: f.value } } };
    default:
      if (!f.value?.trim()) return { problem: `${label}: choose a value.` };
      return { spec: { ...base, fields: { value: f.value } } };
  }
}

export interface UseCustomFormatDraftReturn {
  name: string;
  setName: (v: string) => void;
  includeInRename: boolean;
  setIncludeInRename: (v: boolean) => void;
  specs: SpecDraft[];
  addSpec: () => void;
  removeSpec: (key: number) => void;
  patchSpec: (key: number, change: Partial<Pick<SpecDraft, 'name' | 'negate' | 'required'>>) => void;
  setSpecType: (key: number, implementation: string) => void;
  setSpecField: (key: number, field: string, value: string) => void;
  problem: string | null;
  toInput: () => CustomFormatInput | null;
}

/** Editable copy of one custom format; `firstQuality` seeds new Quality specs. */
export function useCustomFormatDraft(format: CustomFormat | null, firstQuality: string): UseCustomFormatDraftReturn {
  const nextKey = useRef<number>(0);
  const newKey = (): number => {
    nextKey.current += 1;
    return nextKey.current;
  };
  const [name, setName] = useState<string>(format?.name ?? '');
  const [includeInRename, setIncludeInRename] = useState<boolean>(format?.include_in_rename ?? false);
  const [specs, setSpecs] = useState<SpecDraft[]>(() => (format?.specifications ?? []).map((s) => fromSpec(s, newKey())));

  const update = useCallback((key: number, fn: (s: SpecDraft) => SpecDraft): void => {
    setSpecs((prev) => prev.map((s) => (s.key === key ? fn(s) : s)));
  }, []);

  const addSpec = useCallback((): void => {
    const implementation = SPEC_TYPES[0].implementation;
    nextKey.current += 1;
    const key = nextKey.current;
    setSpecs((prev) => [
      ...prev,
      { key, name: '', implementation, negate: false, required: false, fields: defaultFields(implementation, firstQuality), raw: {}, supported: true },
    ]);
  }, [firstQuality]);

  const removeSpec = useCallback((key: number): void => setSpecs((prev) => prev.filter((s) => s.key !== key)), []);
  const patchSpec = useCallback<UseCustomFormatDraftReturn['patchSpec']>((key, change) => update(key, (s) => ({ ...s, ...change })), [update]);
  const setSpecType = useCallback(
    (key: number, implementation: string): void => update(key, (s) => ({ ...s, implementation, fields: defaultFields(implementation, firstQuality) })),
    [update, firstQuality]
  );
  const setSpecField = useCallback(
    (key: number, field: string, value: string): void => update(key, (s) => ({ ...s, fields: { ...s.fields, [field]: value } })),
    [update]
  );

  const built = useMemo((): { input: CustomFormatInput | null; problem: string | null } => {
    if (!name.trim()) return { input: null, problem: 'Give the format a name.' };
    const out: CustomFormatInput['specifications'] = [];
    for (let i = 0; i < specs.length; i += 1) {
      const r = build(specs[i], `Specification ${i + 1}`);
      if ('problem' in r) return { input: null, problem: r.problem };
      out.push(r.spec);
    }
    return { problem: null, input: { name: name.trim(), include_in_rename: includeInRename, specifications: out } };
  }, [name, includeInRename, specs]);

  const toInput = useCallback((): CustomFormatInput | null => built.input, [built]);

  return { name, setName, includeInRename, setIncludeInRename, specs, addSpec, removeSpec, patchSpec, setSpecType, setSpecField, problem: built.problem, toInput };
}
