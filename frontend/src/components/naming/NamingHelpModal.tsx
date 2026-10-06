import React from 'react';
import { ObsidianModal } from '@/components/ui/ObsidianModal';
import type { NamingSyntaxHelp, NamingTokenGroup } from '@/types/naming';

export interface NamingHelpModalProps {
  isOpen: boolean;
  onClose: () => void;
  tokenHelp: NamingTokenGroup[];
  syntaxHelp: NamingSyntaxHelp[];
}

const CODE = 'font-mono text-[#e5a00d] whitespace-pre-wrap break-all';

/** "?" reference: every accepted token / syntax form, what it means in plain language, and an example result. */
export const NamingHelpModal: React.FC<NamingHelpModalProps> = ({ isOpen, onClose, tokenHelp, syntaxHelp }) => (
  <ObsidianModal
    isOpen={isOpen}
    onClose={onClose}
    title="Naming Tokens"
    subtitle="What you can type in a format, and what it becomes"
    maxWidth="sm:max-w-3xl"
  >
    <div className="space-y-6">
      {syntaxHelp.length > 0 && (
        <section>
          <h5 className="text-xs uppercase font-mono tracking-wider text-neutral-300 mb-2">Syntax</h5>
          <dl className="space-y-2">
            {syntaxHelp.map((s) => (
              <div key={s.syntax} className="text-xs">
                <dt className={CODE}>{s.syntax}</dt>
                <dd className="text-neutral-300">{s.description}</dd>
                <dd className="text-neutral-500 font-mono">e.g. {s.example}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      {tokenHelp.map((group) => (
        <section key={group.group}>
          <h5 className="text-xs uppercase font-mono tracking-wider text-neutral-300 mb-2">{group.group}</h5>
          <dl className="space-y-2.5">
            {group.tokens.map((t) => (
              <div key={t.token} className="text-xs">
                <dt className={CODE}>{t.token}</dt>
                <dd className="text-neutral-300">{t.description}</dd>
                <dd className="text-emerald-300 font-mono break-all">{t.example}</dd>
              </div>
            ))}
          </dl>
        </section>
      ))}

      {tokenHelp.length === 0 && (
        <p className="text-xs text-neutral-500 font-mono">Token reference is unavailable (could not reach the server).</p>
      )}
    </div>
  </ObsidianModal>
);
