import React from 'react';
import { Loader2, Save } from 'lucide-react';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';

export interface EditorModalShellProps {
  title: string;
  subtitle?: string;
  maxWidth?: string;
  onClose: () => void;
  /** id of the `<form>` the Save key submits. */
  formId: string;
  saving: boolean;
  /** Blocks Save (client-side validation failed). */
  disabled: boolean;
  /** Validation or server message shown in the footer. */
  error: string | null;
  children: React.ReactNode;
}

/** Full-screen-on-mobile modal with Cancel / Save keys and an inline error line. */
export const EditorModalShell: React.FC<EditorModalShellProps> = ({
  title,
  subtitle,
  maxWidth,
  onClose,
  formId,
  saving,
  disabled,
  error,
  children,
}) => (
  <ObsidianModal
    isOpen
    onClose={onClose}
    title={title}
    subtitle={subtitle}
    maxWidth={maxWidth}
    footer={
      <>
        {error && (
          <p role="alert" className="col-span-full mr-auto self-center text-[11px] font-mono text-[var(--status-error)]">
            {error}
          </p>
        )}
        <TapeDeckButton type="button" onClick={onClose} disabled={saving}>
          Cancel
        </TapeDeckButton>
        <TapeDeckButton
          type="submit"
          form={formId}
          variant="amber"
          disabled={disabled || saving}
          icon={saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
        >
          Save
        </TapeDeckButton>
      </>
    }
  >
    {children}
  </ObsidianModal>
);
