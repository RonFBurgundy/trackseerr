import React from 'react';
import { Download, Loader2 } from 'lucide-react';
import { CopyBox, ObsidianModal, TapeDeckButton } from '@/components/ui';
import type { CustomFormat } from '@/types/customFormats';
import { useCustomFormatExport } from '@/hooks/useCustomFormatExport';

export interface CustomFormatExportModalProps {
  format: CustomFormat | null;
  exportText: (id: number) => Promise<string | null>;
  onClose: () => void;
}

function download(filename: string, json: string): void {
  const url = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

const fileName = (name: string): string => `${name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'custom-format'}.json`;

/** Lidarr-schema JSON of one format: copy it to the clipboard or download it as .json. */
export const CustomFormatExportModal: React.FC<CustomFormatExportModalProps> = ({ format, exportText, onClose }) => {
  const { json, loading } = useCustomFormatExport(format?.id ?? null, exportText);
  if (!format) return null;
  return (
    <ObsidianModal
      isOpen
      onClose={onClose}
      title={`Export ${format.name}`}
      subtitle="Lidarr / Servarr custom-format JSON. Import it into another instance or share it."
      footer={
        <>
          <TapeDeckButton type="button" onClick={onClose}>
            Close
          </TapeDeckButton>
          <TapeDeckButton
            type="button"
            variant="amber"
            disabled={json === null}
            onClick={() => json !== null && download(fileName(format.name), json)}
            icon={<Download className="h-4 w-4" />}
          >
            Download .json
          </TapeDeckButton>
        </>
      }
    >
      {loading || json === null ? (
        <p className="flex items-center gap-2 text-xs font-mono text-neutral-400">
          {loading && <Loader2 className="h-4 w-4 animate-spin" />}
          {loading ? 'Preparing export...' : 'Export unavailable.'}
        </p>
      ) : (
        <CopyBox value={json} label="JSON" multiline className="[&_code]:max-h-72 [&_code]:overflow-y-auto" />
      )}
    </ObsidianModal>
  );
};
