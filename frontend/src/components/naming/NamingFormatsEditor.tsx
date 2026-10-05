import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, HelpCircle, Loader2, Wand2 } from 'lucide-react';
import { NamingHelpModal } from './NamingHelpModal';
import { getNamingPresets, previewNamingFormats } from '@/services/namingService';
import type {
  NamingFormatKey,
  NamingFormatPreview,
  NamingFormats,
  NamingPreset,
  NamingPresetCatalog,
  NamingPreviewContext,
} from '@/types/naming';

interface FieldSpec {
  key: NamingFormatKey;
  label: string;
  hint: string;
  placeholder: string;
  tokens: string[];
}

const FIELDS: FieldSpec[] = [
  {
    key: 'artist_folder_format',
    label: 'Artist Folder Format',
    hint: 'One folder under the library root.',
    placeholder: '{Artist CleanName}',
    tokens: ['{Artist Name}', '{Artist CleanName}', '{ (Artist Disambiguation)}'],
  },
  {
    key: 'standard_track_format',
    label: 'Standard Track Format',
    hint: "Path below the artist folder: album folder(s) '/' file name. Used for single-disc releases.",
    placeholder: '{Album Title}{ - [{Album Type}]}{ (Release Year)}/{track:00} - {Track Title}',
    tokens: [
      '{Album Title}',
      '{Album CleanTitle}',
      '{ - [{Album Type}]}',
      '{ (Release Year)}',
      '{ [Album Disambiguation]}',
      '/',
      '{track:00}',
      '{Track Title}',
      '{Artist Name}',
      '{ [Quality Full]}',
    ],
  },
  {
    key: 'multi_disc_track_format',
    label: 'Multi Disc Track Format',
    hint: 'Same as above, used when the release has more than one disc.',
    placeholder:
      '{Album Title}{ - [{Album Type}]}{ (Release Year)}/Disc {medium:00}/{track:00} - {Track Title}',
    tokens: [
      '{Album Title}',
      '{ - [{Album Type}]}',
      '{ (Release Year)}',
      '/',
      'Disc {medium:00}',
      '{Medium Format} {medium:00}',
      '{medium:0}',
      '{track:00}',
      '{Track Title}',
    ],
  },
];

const INPUT_CLASS =
  'w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d] font-mono';
const LABEL_CLASS = 'block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5';
const PREVIEW_DEBOUNCE_MS = 300;

/** Fields a preset may set; deliberately excludes the library root so presets never move your library. */
function presetPatch(preset: NamingPreset): Partial<NamingPreset> {
  const patch: Partial<NamingPreset> = {
    artist_folder_format: preset.artist_folder_format,
    standard_track_format: preset.standard_track_format,
    multi_disc_track_format: preset.multi_disc_track_format,
    compilation_track_format: preset.compilation_track_format ?? '',
  };
  if (preset.colon_replacement_format !== undefined) patch.colon_replacement_format = preset.colon_replacement_format;
  if (preset.clean_artist_names !== undefined) patch.clean_artist_names = preset.clean_artist_names;
  return patch;
}

export interface NamingFormatsEditorProps {
  value: NamingFormats;
  /** Other settings that change how formats render (root path, colon replacement, clean names). */
  context?: NamingPreviewContext;
  onChange: (patch: Partial<NamingPreset>) => void;
}

/**
 * Lidarr-style naming editor: artist folder, standard track and multi-disc track formats with
 * token shortcuts, presets (Trackseerr / TRaSH Guides / Plex / ...) and a live, server-rendered
 * preview of each format against several sample inputs.
 */
export const NamingFormatsEditor: React.FC<NamingFormatsEditorProps> = ({ value, context, onChange }) => {
  const [catalog, setCatalog] = useState<NamingPresetCatalog>({
    presets: {},
    descriptions: {},
    tokenHelp: [],
    syntaxHelp: [],
  });
  const [helpOpen, setHelpOpen] = useState(false);
  const [previews, setPreviews] = useState<Partial<Record<NamingFormatKey, NamingFormatPreview>>>({});
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [isPreviewing, setIsPreviewing] = useState(false);
  const inputRefs = useRef<Partial<Record<NamingFormatKey, HTMLInputElement | null>>>({});
  const requestSeq = useRef(0);

  useEffect(() => {
    let cancelled = false;
    getNamingPresets()
      .then((c) => {
        if (!cancelled) setCatalog(c);
      })
      .catch(() => {
        /* presets are a convenience; the editor still works without them */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const contextKey = JSON.stringify(context ?? {});
  const compilation = value.compilation_track_format ?? '';

  useEffect(() => {
    const seq = ++requestSeq.current;
    const handle = window.setTimeout(() => {
      setIsPreviewing(true);
      previewNamingFormats(
        {
          artist_folder_format: value.artist_folder_format,
          standard_track_format: value.standard_track_format,
          multi_disc_track_format: value.multi_disc_track_format,
          compilation_track_format: compilation,
        },
        JSON.parse(contextKey) as NamingPreviewContext
      )
        .then((res) => {
          if (seq !== requestSeq.current) return; // a newer edit superseded this response
          setPreviews(res.format_previews);
          setPreviewError(null);
        })
        .catch((err) => {
          if (seq === requestSeq.current) setPreviewError(err instanceof Error ? err.message : 'Preview failed');
        })
        .finally(() => {
          if (seq === requestSeq.current) setIsPreviewing(false);
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [
    value.artist_folder_format,
    value.standard_track_format,
    value.multi_disc_track_format,
    compilation,
    contextKey,
  ]);

  const activePreset = useMemo(
    () =>
      Object.entries(catalog.presets).find(
        ([, p]) =>
          p.artist_folder_format === value.artist_folder_format &&
          p.standard_track_format === value.standard_track_format &&
          p.multi_disc_track_format === value.multi_disc_track_format
      )?.[0] ?? null,
    [catalog.presets, value.artist_folder_format, value.standard_track_format, value.multi_disc_track_format]
  );

  const insertToken = useCallback(
    (key: NamingFormatKey, token: string) => {
      const input = inputRefs.current[key];
      const current = value[key] ?? '';
      const start = input?.selectionStart ?? current.length;
      const end = input?.selectionEnd ?? current.length;
      onChange({ [key]: current.slice(0, start) + token + current.slice(end) });
      const caret = start + token.length;
      window.requestAnimationFrame(() => {
        input?.focus();
        input?.setSelectionRange(caret, caret);
      });
    },
    [onChange, value]
  );

  return (
    <div className="space-y-5">
      <div>
        <div className="flex items-center gap-2 mb-2">
          <Wand2 className="h-3.5 w-3.5 text-[#e5a00d]" />
          <span className="text-xs uppercase font-mono tracking-wider text-neutral-300">Presets</span>
          <button
            type="button"
            onClick={() => setHelpOpen(true)}
            aria-label="Naming token help"
            title="Show accepted tokens and what they produce"
            className="ml-auto flex items-center gap-1 text-[11px] font-mono text-neutral-400 hover:text-[#e5a00d]"
          >
            <HelpCircle className="h-4 w-4" />
            Tokens
          </button>
        </div>
        <div className="flex flex-wrap gap-2">
          {Object.entries(catalog.presets).map(([name, preset]) => (
            <button
              key={name}
              type="button"
              title={catalog.descriptions[name]}
              onClick={() => onChange(presetPatch(preset))}
              className={`text-[11px] font-mono px-2.5 py-1 rounded-[3px] border transition-colors ${
                activePreset === name
                  ? 'border-[#e5a00d] text-[#e5a00d] bg-[#e5a00d]/10'
                  : 'border-[#2a2a2a] text-neutral-300 hover:border-[#e5a00d]/60'
              }`}
            >
              {name}
            </button>
          ))}
        </div>
        {activePreset && catalog.descriptions[activePreset] && (
          <p className="text-[11px] text-neutral-500 font-mono mt-1.5">{catalog.descriptions[activePreset]}</p>
        )}
      </div>

      {FIELDS.map((field) => {
        const preview = previews[field.key];
        return (
          <div key={field.key} className="space-y-2">
            <div>
              <label htmlFor={`naming-${field.key}`} className={LABEL_CLASS}>
                {field.label}
              </label>
              <input
                id={`naming-${field.key}`}
                name={field.key}
                ref={(el) => {
                  inputRefs.current[field.key] = el;
                }}
                type="text"
                spellCheck={false}
                value={value[field.key] ?? ''}
                onChange={(e) => onChange({ [field.key]: e.target.value })}
                placeholder={field.placeholder}
                className={INPUT_CLASS}
              />
              <p className="text-[11px] text-neutral-500 font-mono mt-1">{field.hint}</p>
              <div className="flex flex-wrap gap-1 mt-1.5">
                {field.tokens.map((token) => (
                  <button
                    key={token}
                    type="button"
                    onClick={() => insertToken(field.key, token)}
                    className="text-[10px] font-mono px-1.5 py-0.5 rounded-[3px] bg-[#161616] border border-[#2a2a2a] text-neutral-400 hover:text-white hover:border-[#e5a00d]/60"
                  >
                    {token}
                  </button>
                ))}
              </div>
            </div>

            {preview && (
              <div className="rounded-[3px] border border-[#1f1f1f] bg-[#0a0a0a] p-2.5 space-y-1.5">
                {preview.warnings.map((w) => (
                  <div key={w} className="flex items-start gap-1.5 text-[11px] text-amber-400 font-mono">
                    <AlertTriangle className="h-3 w-3 mt-0.5 shrink-0" />
                    <span>{w}</span>
                  </div>
                ))}
                {preview.samples.map((s) => (
                  <div key={s.sample_id} className="text-[11px] font-mono leading-snug">
                    <span className="text-neutral-500">{s.sample_name}</span>
                    <div className="text-emerald-300 break-all">{s.output || '(empty)'}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}

      <div>
        <label htmlFor="naming-compilation_track_format" className={LABEL_CLASS}>
          Compilation File Name (optional)
        </label>
        <input
          id="naming-compilation_track_format"
          name="compilation_track_format"
          type="text"
          spellCheck={false}
          value={compilation}
          onChange={(e) => onChange({ compilation_track_format: e.target.value })}
          placeholder="{track:00} - {Artist Name} - {Track Title}"
          className={INPUT_CLASS}
        />
        <p className="text-[11px] text-neutral-500 font-mono mt-1">
          Overrides only the file name for Various Artists / compilation releases. Leave empty to use the track
          formats above.
        </p>
      </div>

      <NamingHelpModal
        isOpen={helpOpen}
        onClose={() => setHelpOpen(false)}
        tokenHelp={catalog.tokenHelp}
        syntaxHelp={catalog.syntaxHelp}
      />

      <div className="flex items-center gap-2 text-[11px] font-mono min-h-[1rem]">
        {isPreviewing && <Loader2 className="h-3 w-3 animate-spin text-neutral-500" />}
        {previewError && <span className="text-red-400">{previewError}</span>}
      </div>
    </div>
  );
};
