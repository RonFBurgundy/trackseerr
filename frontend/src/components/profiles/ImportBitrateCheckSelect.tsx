import React, { useId } from 'react';
import { FormField, MachinedCard } from '@/components/ui';
import { inputClass } from '@/components/settings/formClasses';
import { useImportBitrateCheck } from '@/hooks/useImportBitrateCheck';
import type { ImportBitrateCheck } from '@/types/models';

export interface ImportBitrateCheckSelectProps {
  enabled: boolean;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

const OPTIONS: ReadonlyArray<{ value: ImportBitrateCheck; label: string }> = [
  { value: 'off', label: 'Off' },
  { value: 'warn', label: 'Warn (record in events)' },
  { value: 'reject', label: 'Reject (fail the import)' },
];

/** Settings > Media Management > Quality: how a file outside its quality's kbps range is handled on import. */
export const ImportBitrateCheckSelect: React.FC<ImportBitrateCheckSelectProps> = ({ enabled, onToast }) => {
  const uid = useId();
  const { mode, loading, saving, setMode } = useImportBitrateCheck(enabled, onToast);
  return (
    <MachinedCard className="p-3 sm:p-4">
      <FormField
        label="Check each track's bitrate on import"
        htmlFor={`${uid}-import-bitrate-check`}
        hint="Compares every imported file's bitrate with its quality's min/max above. Warn logs files outside the range; Reject fails the import (catches fake FLAC upconverts and truncated files). Files with an unreadable duration are skipped."
      >
        <select
          id={`${uid}-import-bitrate-check`}
          name="import_bitrate_check"
          value={mode}
          disabled={loading || saving}
          onChange={(e) => {
            const v = e.target.value;
            if (v === 'off' || v === 'warn' || v === 'reject') void setMode(v);
          }}
          className={inputClass}
        >
          {OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </FormField>
    </MachinedCard>
  );
};
