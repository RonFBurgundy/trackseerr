import React, { useMemo, useState } from 'react';
import { Loader2, UserPlus } from 'lucide-react';
import {
  MachinedCard,
  SearchBar,
  StatusMessage,
  TapeDeckButton,
  TabStrip,
} from '@/components/ui';
import type { AdminUser, AuthType } from '@/types/account';
import type { UseAdminUsersReturn } from '@/hooks/useAdminUsers';
import { CreateUserModal } from './CreateUserModal';
import { UserEditModal } from './UserEditModal';
import { AccountDefaultsPanel } from './AccountDefaultsPanel';

export interface UsersPanelProps {
  adminHook: UseAdminUsersReturn;
  currentUserId: string | number | undefined;
}

type TypeFilter = 'all' | AuthType;
type StatusFilter = 'all' | 'active' | 'disabled';

const Badge: React.FC<{ tone: 'ok' | 'warn' | 'muted' | 'amber'; children: React.ReactNode }> = ({
  tone,
  children,
}) => {
  const cls = {
    ok: 'border-[var(--status-success)] text-[var(--status-success)]',
    warn: 'border-[var(--status-error)] text-[var(--status-error)]',
    muted: 'border-[var(--border-default)] text-[var(--text-muted)]',
    amber: 'border-[var(--accent-amber)] text-[var(--accent-amber)]',
  }[tone];
  return (
    <span className={`inline-block px-1.5 py-0.5 rounded-[2px] border text-[10px] font-mono uppercase ${cls}`}>
      {children}
    </span>
  );
};

function formatDate(iso: string | null): string {
  if (!iso) return 'Never';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString();
}

export const UsersPanel: React.FC<UsersPanelProps> = ({ adminHook, currentUserId }) => {
  const { users, defaults, isLoading, error } = adminHook;
  const [section, setSection] = useState<'users' | 'defaults'>('users');
  const [search, setSearch] = useState<string>('');
  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [createOpen, setCreateOpen] = useState<boolean>(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return users.filter((u) => {
      if (typeFilter !== 'all' && u.auth_type !== typeFilter) return false;
      if (statusFilter === 'active' && u.disabled) return false;
      if (statusFilter === 'disabled' && !u.disabled) return false;
      if (!q) return true;
      return u.username.toLowerCase().includes(q) || (u.email ?? '').toLowerCase().includes(q);
    });
  }, [users, search, typeFilter, statusFilter]);

  const editing: AdminUser | null = users.find((u) => u.id === editingId) ?? null;

  return (
    <div className="space-y-4">
      <div className="flex items-stretch justify-between gap-2 sm:gap-3">
        <TabStrip className="min-w-0 flex-1 sm:flex-none" fill>
          <TapeDeckButton size="sm" active={section === 'users'} onClick={() => setSection('users')}>
            Users
          </TapeDeckButton>
          <TapeDeckButton
            size="sm"
            active={section === 'defaults'}
            onClick={() => setSection('defaults')}
          >
            Defaults
          </TapeDeckButton>
        </TabStrip>
        {section === 'users' && (
          <TapeDeckButton
            size="sm"
            variant="amber"
            onClick={() => setCreateOpen(true)}
            icon={<UserPlus className="h-3.5 w-3.5" />}
            collapseLabel
          >
            Create local user
          </TapeDeckButton>
        )}
      </div>

      {error && <StatusMessage variant="error">{error}</StatusMessage>}

      {section === 'defaults' && (
        <AccountDefaultsPanel defaults={defaults} onSave={adminHook.saveDefaults} />
      )}

      {section === 'users' && (
        <>
          <div className="flex flex-col lg:flex-row gap-3 lg:items-center">
            <div className="flex-1 max-w-lg">
              <SearchBar value={search} onChange={setSearch} placeholder="Search users..." />
            </div>
            <div className="flex flex-col sm:flex-row gap-2 sm:gap-3">
              <TabStrip fill>
                {(['all', 'plex', 'local', 'jellyfin'] as const).map((t) => (
                  <TapeDeckButton
                    key={t}
                    size="sm"
                    active={typeFilter === t}
                    onClick={() => setTypeFilter(t)}
                  >
                    {t === 'all' ? 'All types' : t}
                  </TapeDeckButton>
                ))}
              </TabStrip>
              <TabStrip fill>
                {(['all', 'active', 'disabled'] as const).map((s) => (
                  <TapeDeckButton
                    key={s}
                    size="sm"
                    active={statusFilter === s}
                    onClick={() => setStatusFilter(s)}
                  >
                    {s === 'all' ? 'Any status' : s}
                  </TapeDeckButton>
                ))}
              </TabStrip>
            </div>
          </div>

          {isLoading && users.length === 0 ? (
            <div className="flex justify-center py-12">
              <Loader2 className="h-8 w-8 text-[var(--accent-amber)] animate-spin" />
            </div>
          ) : (
            <MachinedCard className="overflow-x-auto">
              <table className="w-full text-left text-xs font-mono">
                <thead>
                  <tr className="border-b border-[var(--border-subtle)] text-[var(--text-muted)] uppercase">
                    <th className="px-3 py-2 font-medium">User</th>
                    <th className="px-3 py-2 font-medium">Type</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2 font-medium">MFA</th>
                    <th className="px-3 py-2 font-medium">Last login</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((u) => (
                    <tr
                      key={u.id}
                      onClick={() => setEditingId(u.id)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          setEditingId(u.id);
                        }
                      }}
                      tabIndex={0}
                      className="border-b border-[var(--border-subtle)] last:border-0 cursor-pointer hover:bg-[var(--bg-card-hover)] focus:outline-none focus:bg-[var(--bg-card-hover)] min-h-[36px]"
                    >
                      <td className="px-3 py-2 md:py-3">
                        <div className="text-[var(--text-primary)]">
                          {u.username}
                          {String(currentUserId) === u.id && (
                            <span className="text-[var(--text-muted)]"> (you)</span>
                          )}
                        </div>
                        {u.email && <div className="text-[var(--text-muted)]">{u.email}</div>}
                      </td>
                      <td className="px-3 py-2 md:py-3">
                        <Badge tone={u.auth_type === 'local' ? 'amber' : 'muted'}>{u.auth_type}</Badge>
                        {u.is_admin && (
                          <>
                            {' '}
                            <Badge tone="amber">admin</Badge>
                          </>
                        )}
                      </td>
                      <td className="px-3 py-2 md:py-3">
                        <Badge tone={u.disabled ? 'warn' : 'ok'}>
                          {u.disabled ? 'disabled' : 'active'}
                        </Badge>
                      </td>
                      <td className="px-3 py-2 md:py-3">
                        {u.auth_type === 'local' ? (
                          <Badge tone={u.mfa_enabled ? 'ok' : 'muted'}>
                            {u.mfa_enabled ? 'on' : 'off'}
                          </Badge>
                        ) : (
                          <span className="text-[var(--text-muted)]">n/a</span>
                        )}
                      </td>
                      <td className="px-3 py-2 md:py-3 text-[var(--text-secondary)]">
                        {formatDate(u.last_login_at)}
                      </td>
                    </tr>
                  ))}
                  {filtered.length === 0 && (
                    <tr>
                      <td colSpan={5} className="px-3 py-8 text-center text-[var(--text-muted)]">
                        No users match.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </MachinedCard>
          )}
        </>
      )}

      <CreateUserModal
        isOpen={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreate={adminHook.create}
      />
      <UserEditModal
        user={editing}
        currentUserId={currentUserId}
        adminHook={adminHook}
        onClose={() => setEditingId(null)}
      />
    </div>
  );
};
