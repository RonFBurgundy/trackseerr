import React, { useState } from 'react';
import { Loader2, Send } from 'lucide-react';
import { FormField, inputClass, TapeDeckButton } from '@/components/ui';
import { ISSUE_MAX_COMMENT } from '@/types/models';

export interface IssueComposerProps {
  disabled?: boolean;
  /** Posts the comment; resolves to an error message, or null on success (which clears the box). */
  onSend: (body: string) => Promise<string | null>;
}

export const IssueComposer: React.FC<IssueComposerProps> = ({ disabled = false, onSend }) => {
  const [body, setBody] = useState<string>('');
  const [sending, setSending] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const trimmed = body.trim();
  const tooLong = body.length > ISSUE_MAX_COMMENT;

  const send = async (): Promise<void> => {
    if (!trimmed || tooLong || sending) return;
    setSending(true);
    setError(null);
    const failure = await onSend(trimmed);
    setSending(false);
    if (failure) setError(failure);
    else setBody('');
  };

  return (
    <div className="space-y-2">
      <FormField label="Add a comment" name="issue-comment">
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          disabled={disabled || sending}
          rows={3}
          className={`${inputClass} resize-y`}
        />
      </FormField>
      {error && (
        <p role="alert" className="text-xs font-mono text-[var(--status-error)]">
          {error}
        </p>
      )}
      <div className="flex items-center justify-between gap-2">
        <span className={`text-[11px] font-mono ${tooLong ? 'text-[var(--status-error)]' : 'text-[var(--text-muted)]'}`}>
          {body.length} / {ISSUE_MAX_COMMENT}
        </span>
        <TapeDeckButton
          size="sm"
          variant="amber"
          disabled={disabled || sending || !trimmed || tooLong}
          onClick={() => void send()}
          icon={sending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
        >
          Comment
        </TapeDeckButton>
      </div>
    </div>
  );
};
