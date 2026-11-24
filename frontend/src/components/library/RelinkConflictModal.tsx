import React from 'react';
import { Loader2 } from 'lucide-react';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import type { RelinkConflict } from '@/hooks/useRenameRelink';
import type { RenameRelinkFile } from '@/services/renameService';

const LOSSLESS_CODECS = new Set(['flac', 'alac', 'wav', 'aiff', 'ape', 'wv']);

/** Positive when `a` is the higher-quality file, negative when `b` is, 0 on a tie. */
function compareQuality(a: RenameRelinkFile, b: RenameRelinkFile): number {
  const lossless = (f: RenameRelinkFile): number =>
    LOSSLESS_CODECS.has((f.codec ?? '').toLowerCase()) ? 1 : 0;
  const steps: number[] = [
    lossless(a) - lossless(b),
    (a.bits_per_sample ?? 0) - (b.bits_per_sample ?? 0),
    (a.sample_rate ?? 0) - (b.sample_rate ?? 0),
    (a.bitrate ?? 0) - (b.bitrate ?? 0),
    (a.size_bytes ?? 0) - (b.size_bytes ?? 0),
  ];
  return steps.find((n) => n !== 0) ?? 0;
}

function baseName(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

function fileFacts(file: RenameRelinkFile): string[] {
  const facts: string[] = [];
  const format = [file.quality_name, file.codec].filter((v): v is string => !!v);
  if (format.length > 0) facts.push(format.join(' / '));
  if (file.bitrate != null) facts.push(`${Math.round(file.bitrate)} kbps`);
  const depth = [
    file.bits_per_sample != null ? `${file.bits_per_sample}-bit` : null,
    file.sample_rate != null ? `${Number((file.sample_rate / 1000).toFixed(1))} kHz` : null,
  ].filter((v): v is string => v !== null);
  if (depth.length > 0) facts.push(depth.join(' / '));
  if (file.size_bytes != null) facts.push(`${(file.size_bytes / (1024 * 1024)).toFixed(1)} MB`);
  return facts;
}

const RelinkFileCard: React.FC<{ heading: string; file: RenameRelinkFile; better: boolean }> = ({
  heading,
  file,
  better,
}) => (
  <div className="min-w-0 space-y-1.5 rounded-[4px] border border-[var(--border-default)] bg-[var(--bg-card)] p-3 font-mono text-xs">
    <div className="flex items-center justify-between gap-2">
      <span className="text-[10px] font-bold uppercase tracking-widest text-[var(--text-secondary)]">{heading}</span>
      {better && (
        <span className="rounded-[3px] border border-[#e5a00d]/50 bg-[#e5a00d]/10 px-1.5 py-0.5 text-[10px] leading-none text-[#ffc94d]">
          Higher quality
        </span>
      )}
    </div>
    <p className="break-all text-white" title={file.file_path}>
      {baseName(file.file_path)}
    </p>
    <ul className="space-y-0.5 text-[var(--text-secondary)]">
      {fileFacts(file).map((fact) => (
        <li key={fact}>{fact}</li>
      ))}
    </ul>
  </div>
);

export interface RelinkConflictModalProps {
  conflict: RelinkConflict | null;
  busy: boolean;
  onCancel: () => void;
  onKeepExisting: () => void;
  onKeepIncoming: () => void;
}

export const RelinkConflictModal: React.FC<RelinkConflictModalProps> = ({
  conflict,
  busy,
  onCancel,
  onKeepExisting,
  onKeepIncoming,
}) => {
  const conflictOrder = conflict ? compareQuality(conflict.existing, conflict.incoming) : 0;

  return (
    <ObsidianModal
      isOpen={conflict !== null}
      onClose={onCancel}
      title="The right track already has a file"
      subtitle="Choose which file stays linked to that track"
      maxWidth="sm:max-w-2xl"
      footer={
        <>
          <TapeDeckButton onClick={onCancel} disabled={busy}>
            Cancel
          </TapeDeckButton>
          <TapeDeckButton disabled={busy} onClick={onKeepExisting}>
            Keep existing
          </TapeDeckButton>
          <TapeDeckButton
            variant="amber"
            disabled={busy}
            onClick={onKeepIncoming}
            icon={busy ? <Loader2 className="h-4 w-4 animate-spin" /> : undefined}
          >
            Keep this file
          </TapeDeckButton>
        </>
      }
    >
      {conflict && (
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <RelinkFileCard heading="Already on that track" file={conflict.existing} better={conflictOrder > 0} />
            <RelinkFileCard heading="This file" file={conflict.incoming} better={conflictOrder < 0} />
          </div>
          <p className="font-mono text-xs text-[var(--text-secondary)]">
            The file you don&apos;t keep goes to the recycle bin.
          </p>
        </div>
      )}
    </ObsidianModal>
  );
};
