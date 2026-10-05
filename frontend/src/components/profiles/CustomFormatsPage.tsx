import React, { useState } from 'react';
import { Download, Pencil, Plus, Trash2, Upload } from 'lucide-react';
import { ConfirmDialog, MachinedCard, ScrollFill, TapeDeckButton } from '@/components/ui';
import type { CustomFormat } from '@/types/customFormats';
import { useCustomFormats } from '@/hooks/useCustomFormats';
import { useQualityDefinitions } from '@/hooks/useQualityDefinitions';
import { CustomFormatEditorModal, type CustomFormatTarget } from './CustomFormatEditorModal';
import { CustomFormatExportModal } from './CustomFormatExportModal';
import { CustomFormatImportModal } from './CustomFormatImportModal';
import { Badge, EmptyNote } from './ProfileSection';

export interface CustomFormatsPageProps {
  enabled: boolean;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Settings > Media Management > Custom Formats: card grid, editor, JSON import and export. */
export const CustomFormatsPage: React.FC<CustomFormatsPageProps> = ({ enabled, onToast }) => {
  const manager = useCustomFormats(enabled, onToast);
  const { definitions } = useQualityDefinitions(enabled, onToast);
  const [editing, setEditing] = useState<CustomFormatTarget | null>(null);
  const [importing, setImporting] = useState<boolean>(false);
  const [exporting, setExporting] = useState<CustomFormat | null>(null);
  const [deleting, setDeleting] = useState<CustomFormat | null>(null);

  const confirmDelete = async (): Promise<void> => {
    if (deleting) await manager.remove(deleting.id);
    setDeleting(null);
  };

  return (
    <div className="flex min-h-0 flex-col gap-3">
      <MachinedCard className="flex flex-col gap-3 p-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-xs font-mono text-neutral-400">
          Custom formats tag releases by title, group, size, source and more. Give them scores in a quality profile; they import from Lidarr/Servarr
          JSON.
        </p>
        <div className="flex shrink-0 gap-2">
          <TapeDeckButton size="sm" onClick={() => setImporting(true)} icon={<Upload className="h-3.5 w-3.5" />}>
            Import
          </TapeDeckButton>
          <TapeDeckButton size="sm" variant="amber" onClick={() => setEditing('new')} icon={<Plus className="h-3.5 w-3.5" />}>
            Add
          </TapeDeckButton>
        </div>
      </MachinedCard>

      {manager.loading && manager.formats.length === 0 ? (
        <EmptyNote>Loading custom formats...</EmptyNote>
      ) : manager.formats.length === 0 ? (
        <EmptyNote>No custom formats yet. Add one or import a JSON file.</EmptyNote>
      ) : (
        <ScrollFill ariaLabel="Custom formats" className="grid grid-cols-1 content-start gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
          {manager.formats.map((f) => (
            <MachinedCard key={f.id} className="space-y-2 p-2.5 sm:p-3">
              <div className="min-w-0">
                <p className="break-words text-sm font-bold text-white">{f.name}</p>
                <div className="mt-1 flex flex-wrap items-center gap-1.5">
                  <Badge>
                    {f.specifications.length} {f.specifications.length === 1 ? 'spec' : 'specs'}
                  </Badge>
                  {f.unsupported && <Badge tone="error">Unsupported</Badge>}
                  {f.include_in_rename && <Badge>Rename</Badge>}
                </div>
              </div>
              <div className="flex items-center gap-1.5">
                <TapeDeckButton size="sm" aria-label={`Edit ${f.name}`} onClick={() => setEditing(f)} icon={<Pencil className="h-3.5 w-3.5" />} />
                <TapeDeckButton size="sm" aria-label={`Export ${f.name}`} onClick={() => setExporting(f)} icon={<Download className="h-3.5 w-3.5" />} />
                <TapeDeckButton size="sm" variant="danger" aria-label={`Delete ${f.name}`} onClick={() => setDeleting(f)} icon={<Trash2 className="h-3.5 w-3.5" />} />
              </div>
            </MachinedCard>
          ))}
        </ScrollFill>
      )}

      <CustomFormatEditorModal target={editing} qualities={definitions} onClose={() => setEditing(null)} onSave={manager.save} />
      <CustomFormatImportModal open={importing} onClose={() => setImporting(false)} importText={manager.importText} />
      <CustomFormatExportModal format={exporting} exportText={manager.exportText} onClose={() => setExporting(null)} />
      <ConfirmDialog
        isOpen={deleting !== null}
        title="Delete custom format"
        confirmLabel="Delete"
        onConfirm={() => void confirmDelete()}
        onCancel={() => setDeleting(null)}
      >
        Delete &ldquo;{deleting?.name}&rdquo;? Its score is removed from every quality profile that uses it. This cannot be undone.
      </ConfirmDialog>
    </div>
  );
};
