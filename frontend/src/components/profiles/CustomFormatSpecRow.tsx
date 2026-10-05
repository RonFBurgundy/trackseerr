import React, { useId } from 'react';
import { Trash2 } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';
import { SPEC_SOURCES, SPEC_TYPES } from '@/types/customFormats';
import { RELEASE_PROTOCOLS } from '@/types/qualityProfiles';
import type { QualityDefinition } from '@/types/qualityDefinitions';
import type { SpecDraft, UseCustomFormatDraftReturn } from '@/hooks/useCustomFormatDraft';
import { compactInputClass, compactLabelClass } from '@/components/settings/formClasses';
import { Badge } from './ProfileSection';

export interface CustomFormatSpecRowProps {
  spec: SpecDraft;
  index: number;
  qualities: readonly QualityDefinition[];
  draft: Pick<UseCustomFormatDraftReturn, 'patchSpec' | 'setSpecType' | 'setSpecField' | 'removeSpec'>;
}

const checkClass = 'h-5 w-5 accent-[#e5a00d]';

/** One specification: type select, type-specific fields, negate and required. Unsupported specs are read-only. */
export const CustomFormatSpecRow: React.FC<CustomFormatSpecRowProps> = ({ spec, index, qualities, draft }) => {
  const uid = useId();
  const n = index + 1;
  const type = SPEC_TYPES.find((t) => t.implementation === spec.implementation);

  const text = (field: string, label: string, opts: { mono?: boolean; type?: 'text' | 'number'; placeholder?: string } = {}): React.ReactNode => (
    <div className="min-w-0">
      <label htmlFor={`${uid}-${field}`} className={compactLabelClass}>
        {label}
      </label>
      <input
        id={`${uid}-${field}`}
        name={`spec-${spec.key}-${field}`}
        type={opts.type ?? 'text'}
        autoComplete="off"
        step={opts.type === 'number' ? 'any' : undefined}
        placeholder={opts.placeholder}
        value={spec.fields[field] ?? ''}
        onChange={(e) => draft.setSpecField(spec.key, field, e.target.value)}
        className={`${compactInputClass} ${opts.mono ? 'font-mono' : ''}`}
      />
    </div>
  );

  const choice = (label: string, options: ReadonlyArray<{ value: string; label: string }>): React.ReactNode => (
    <div className="min-w-0">
      <label htmlFor={`${uid}-value`} className={compactLabelClass}>
        {label}
      </label>
      <select
        id={`${uid}-value`}
        name={`spec-${spec.key}-value`}
        value={spec.fields.value ?? ''}
        onChange={(e) => draft.setSpecField(spec.key, 'value', e.target.value)}
        className={compactInputClass}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );

  const header = (
    <div className="flex items-center justify-between gap-2">
      <span className="text-[11px] font-mono uppercase tracking-wider text-neutral-400">Specification {n}</span>
      <TapeDeckButton size="sm" variant="danger" aria-label={`Remove specification ${n}`} onClick={() => draft.removeSpec(spec.key)} icon={<Trash2 className="h-3.5 w-3.5" />} />
    </div>
  );

  if (!spec.supported) {
    return (
      <div className="space-y-1.5 rounded-[4px] border border-[var(--border-default)] bg-[var(--bg-surface)] p-2.5">
        {header}
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-mono text-xs text-neutral-300">{spec.implementation}</span>
          <Badge tone="error">Unsupported</Badge>
          {spec.negate && <Badge>Negate</Badge>}
          {spec.required && <Badge>Required</Badge>}
        </div>
        <p className="text-[11px] font-mono text-[var(--text-muted)]">
          TrackSeerr&apos;s engine does not implement this specification type. It is kept so the format exports unchanged, but it is ignored when
          matching releases.
        </p>
        <code className="block max-h-24 overflow-y-auto whitespace-pre-wrap break-all rounded-[3px] bg-[var(--bg-canvas)] p-2 text-[11px] text-neutral-400">
          {JSON.stringify(spec.raw)}
        </code>
      </div>
    );
  }

  return (
    <div className="space-y-2 rounded-[4px] border border-[var(--border-default)] bg-[var(--bg-card)] p-2.5">
      {header}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <div className="min-w-0">
          <label htmlFor={`${uid}-type`} className={compactLabelClass}>
            Type
          </label>
          <select
            id={`${uid}-type`}
            name={`spec-${spec.key}-type`}
            value={spec.implementation}
            onChange={(e) => draft.setSpecType(spec.key, e.target.value)}
            className={compactInputClass}
          >
            {SPEC_TYPES.map((t) => (
              <option key={t.implementation} value={t.implementation}>
                {t.label}
              </option>
            ))}
          </select>
        </div>
        <div className="min-w-0">
          <label htmlFor={`${uid}-name`} className={compactLabelClass}>
            Name (optional)
          </label>
          <input
            id={`${uid}-name`}
            name={`spec-${spec.key}-name`}
            type="text"
            autoComplete="off"
            value={spec.name}
            onChange={(e) => draft.patchSpec(spec.key, { name: e.target.value })}
            className={compactInputClass}
          />
        </div>
      </div>
      {type && <p className="text-[11px] font-mono text-[var(--text-muted)]">{type.hint}</p>}
      <div className="grid grid-cols-2 gap-2">
        {(spec.implementation === 'ReleaseTitleSpecification' || spec.implementation === 'ReleaseGroupSpecification') && (
          <div className="col-span-2">{text('value', 'Regular expression', { mono: true, placeholder: '\\bFLAC\\b' })}</div>
        )}
        {spec.implementation === 'SizeSpecification' && (
          <>
            {text('min', 'Min size (GB)', { type: 'number', mono: true })}
            {text('max', 'Max size (GB)', { type: 'number', mono: true, placeholder: 'no limit' })}
          </>
        )}
        {spec.implementation === 'IndexerFlagSpecification' && text('value', 'Flag value', { type: 'number', mono: true })}
        {spec.implementation === 'QualitySpecification' &&
          choice(
            'Quality',
            qualities.map((q) => ({ value: q.quality, label: q.title }))
          )}
        {spec.implementation === 'SourceSpecification' &&
          choice(
            'Source',
            SPEC_SOURCES.map((s) => ({ value: s, label: s }))
          )}
        {spec.implementation === 'ProtocolSpecification' &&
          choice(
            'Protocol',
            RELEASE_PROTOCOLS.map((p) => ({ value: p, label: p }))
          )}
        {spec.implementation === 'PhraseSpecification' && (
          <>
            {text('value', 'Phrase')}
            {text('threshold', 'Match threshold (1-100)', { type: 'number', mono: true })}
          </>
        )}
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-1">
        <label htmlFor={`${uid}-negate`} className="inline-flex min-h-[32px] items-center gap-2 text-xs font-mono text-neutral-200">
          <input id={`${uid}-negate`} name={`spec-${spec.key}-negate`} type="checkbox" checked={spec.negate} onChange={(e) => draft.patchSpec(spec.key, { negate: e.target.checked })} className={checkClass} />
          Negate (match when it does not)
        </label>
        <label htmlFor={`${uid}-required`} className="inline-flex min-h-[32px] items-center gap-2 text-xs font-mono text-neutral-200">
          <input id={`${uid}-required`} name={`spec-${spec.key}-required`} type="checkbox" checked={spec.required} onChange={(e) => draft.patchSpec(spec.key, { required: e.target.checked })} className={checkClass} />
          Required
        </label>
      </div>
    </div>
  );
};
