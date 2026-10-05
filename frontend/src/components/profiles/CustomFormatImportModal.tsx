import React, { useId, useRef } from 'react';
import { FileUp, Loader2, Upload } from 'lucide-react';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import { useCustomFormatImport } from '@/hooks/useCustomFormatImport';
import type { ImportOutcome } from '@/hooks/useCustomFormats';
import { compactLabelClass } from '@/components/settings/formClasses';
import { Badge } from './ProfileSection';

export interface CustomFormatImportModalProps {
  onClose: () => void;
  importText: (text: string) => Promise<ImportOutcome>;
}

/** Paste or upload Lidarr/Servarr custom-format JSON (one object or a list) and see what was created, updated or refused. */
const ImportBody: React.FC<CustomFormatImportModalProps> = ({ onClose, importText }) => {
  const uid = useId();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const imp = useCustomFormatImport(importText);

  return (
    <ObsidianModal
      isOpen
      onClose={onClose}
      title="Import Custom Formats"
      subtitle="Lidarr / Servarr JSON: one format object or a list. A format with an existing name is replaced."
      footer={
        <>
          <TapeDeckButton type="button" onClick={onClose}>
            Close
          </TapeDeckButton>
          <TapeDeckButton
            type="button"
            variant="amber"
            disabled={imp.busy || imp.text.trim() === ''}
            onClick={() => void imp.submit()}
            icon={imp.busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
          >
            Import
          </TapeDeckButton>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <label htmlFor={`${uid}-json`} className={compactLabelClass}>
            Custom format JSON
          </label>
          <textarea
            id={`${uid}-json`}
            name="custom_format_json"
            rows={10}
            spellCheck={false}
            value={imp.text}
            onChange={(e) => imp.setText(e.target.value)}
            placeholder='{ "name": "Preferred Groups", "specifications": [ ... ] }'
            className="w-full rounded-[3px] border border-[var(--border-default)] bg-[var(--bg-canvas)] p-2 font-mono text-xs text-white focus:border-[var(--accent-amber)] focus:outline-none"
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            ref={fileRef}
            id={`${uid}-file`}
            name="custom_format_file"
            type="file"
            accept=".json,application/json"
            aria-label="Choose a custom format JSON file"
            className="sr-only"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void imp.loadFile(file);
              e.target.value = '';
            }}
          />
          <TapeDeckButton size="sm" onClick={() => fileRef.current?.click()} icon={<FileUp className="h-3.5 w-3.5" />}>
            Choose .json file
          </TapeDeckButton>
        </div>
        {imp.error && (
          <p role="alert" className="text-xs font-mono text-[var(--status-error)]">
            {imp.error}
          </p>
        )}
        {imp.result && (
          <div className="space-y-1 text-xs font-mono" aria-live="polite">
            {imp.result.imported.map((f) => (
              <p key={f.id} className="flex items-center gap-2 text-neutral-200">
                <Badge tone="amber">{f.action}</Badge>
                <span className="truncate">{f.name}</span>
              </p>
            ))}
            {imp.result.errors.map((e) => (
              <div key={`${e.index}:${e.name ?? ''}`} className="text-[var(--status-error)]">
                <p className="flex items-center gap-2">
                  <Badge tone="error">error</Badge>
                  <span className="truncate">
                    #{e.index + 1} {e.name ?? '(unnamed)'}
                  </span>
                </p>
                <ul className="ml-4 list-disc text-[11px]">
                  {e.errors.map((m) => (
                    <li key={m}>{m}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </div>
    </ObsidianModal>
  );
};

export const CustomFormatImportModal: React.FC<CustomFormatImportModalProps & { open: boolean }> = ({ open, ...rest }) =>
  open ? <ImportBody {...rest} /> : null;
