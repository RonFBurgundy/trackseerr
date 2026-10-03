import React, { useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { TapeDeckButton } from './TapeDeckButton';

export interface CopyBoxProps {
  value: string;
  label?: string;
  /** Render the value as pre-wrapped multi-line text (e.g. recovery codes). */
  multiline?: boolean;
  className?: string;
}

export const CopyBox: React.FC<CopyBoxProps> = ({
  value,
  label,
  multiline = false,
  className = '',
}) => {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setState('copied');
    } catch (err: unknown) {
      console.warn('Clipboard write failed', err);
      setState('failed');
    }
    window.setTimeout(() => setState('idle'), 2000);
  };

  return (
    <div className={`space-y-2 ${className}`}>
      {label && (
        <span className="block text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)]">
          {label}
        </span>
      )}
      <div className="flex flex-col sm:flex-row gap-2 sm:items-stretch">
        <code
          className={`flex-1 min-w-0 bg-[var(--bg-canvas)] border border-[var(--border-default)] rounded-[3px] px-3 py-2 text-xs font-mono text-[var(--text-primary)] select-all break-all ${
            multiline ? 'whitespace-pre-wrap' : ''
          }`}
        >
          {value}
        </code>
        <TapeDeckButton
          size="sm"
          type="button"
          onClick={handleCopy}
          icon={state === 'copied' ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
        >
          {state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy'}
        </TapeDeckButton>
      </div>
    </div>
  );
};
