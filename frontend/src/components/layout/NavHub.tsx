import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, X, LogOut, Shield, User as UserIcon } from 'lucide-react';
import type { AppRoute, Navigate } from '@/hooks/useAppRoute';
import type { DeploymentTier, User, UserQuota } from '@/types/models';
import { TapeDeckButton, QuotaBadge } from '@/components/ui';
import { activeAncestorKeys, buildNavTree, routesEqual } from './navTree';
import type { NavNode } from './navTree';

export interface NavHubProps {
  isOpen: boolean;
  onClose: () => void;
  /** Current (gated) location: highlighted in the tree, its ancestors start expanded. */
  route: AppRoute;
  onNavigate: Navigate;
  user: User | null;
  quota: UserQuota | null;
  isAdmin?: boolean;
  mfaEnrollmentRequired?: boolean;
  reviewCount?: number;
  issuesOpenCount?: number;
  issuesUnreadCount?: number;
  onLogout?: () => void;
  tier?: DeploymentTier;
}

const FOCUSABLE = 'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])';

interface NodeRowProps {
  node: NavNode;
  depth: number;
  route: AppRoute;
  expanded: ReadonlySet<string>;
  onToggle: (key: string) => void;
  onGo: (route: AppRoute) => void;
}

/** True when this node, or anything beneath it, is the current location. */
const containsRoute = (node: NavNode, route: AppRoute): boolean =>
  (node.route !== undefined && routesEqual(node.route, route)) ||
  (node.children?.some((child) => containsRoute(child, route)) ?? false);

const NodeRow: React.FC<NodeRowProps> = ({ node, depth, route, expanded, onToggle, onGo }) => {
  const isLeaf = node.route !== undefined && !node.children;
  const here = containsRoute(node, route);
  const isCurrent = isLeaf && node.route !== undefined && routesEqual(node.route, route);
  const isOpen = expanded.has(node.key);
  const panelId = `nav-hub-${node.key.replace(/[^a-z0-9-]/gi, '-')}`;
  const route0 = node.route;

  const rowClass = `w-full flex items-center gap-2.5 rounded-[3px] text-left transition-all duration-75 border ${
    depth === 0 ? 'min-h-[40px] px-2.5 py-1 md:min-h-[48px] md:px-3 md:py-2' : 'min-h-[36px] px-2.5 py-1 md:min-h-[40px]'
  } ${
    here
      ? 'bg-[#151515] border-[#e5a00d]/40 shadow-[inset_0_1px_3px_rgba(0,0,0,0.8)]'
      : 'bg-[#121212] border-[#1e1e1e] hover:border-[#333333] hover:bg-[#181818]'
  }`;

  const body = (
    <>
      {node.icon && (
        <div
          className={`${depth === 0 ? 'w-7 h-7' : 'w-6 h-6'} rounded-[3px] flex items-center justify-center flex-shrink-0 border ${
            here ? 'bg-[#0f0f0f] border-[#e5a00d] text-[#e5a00d]' : 'bg-[#181818] border-[#262626] text-neutral-300'
          }`}
        >
          {node.icon}
        </div>
      )}
      <div className="flex-1 min-w-0">
        <span
          className={`text-[13px] md:text-xs font-bold uppercase tracking-wider font-mono ${here ? 'text-[#e5a00d]' : 'text-neutral-200'}`}
        >
          {node.label}
        </span>
        {depth === 0 && node.description && (
          <p className="hidden md:block text-[11px] text-neutral-400 font-mono truncate">{node.description}</p>
        )}
      </div>
      {node.badge !== undefined && node.badge > 0 && (
        <span className="px-1.5 rounded-[3px] bg-[var(--accent-amber)] text-[10px] font-mono font-bold text-black" aria-label={`${node.badge} needing attention`}>
          {node.badge}
        </span>
      )}
      {isCurrent && (
        <span className="h-1.5 w-1.5 rounded-full bg-[#e5a00d] shadow-[0_0_6px_rgba(229,160,13,0.8)]" aria-hidden="true" />
      )}
      {node.children && (
        <ChevronDown
          className={`h-4 w-4 shrink-0 text-neutral-400 transition-transform ${isOpen ? 'rotate-180' : ''}`}
          aria-hidden="true"
        />
      )}
    </>
  );

  return (
    <li className="flex flex-col gap-0.5">
      {node.children ? (
        <button
          type="button"
          className={rowClass}
          aria-expanded={isOpen}
          aria-controls={panelId}
          onClick={() => onToggle(node.key)}
        >
          {body}
        </button>
      ) : (
        <button
          type="button"
          className={rowClass}
          aria-current={isCurrent ? 'page' : undefined}
          onClick={() => {
            if (route0) onGo(route0);
          }}
        >
          {body}
        </button>
      )}
      {node.children && isOpen && (
        <ul id={panelId} className="flex flex-col gap-0.5 border-l border-[#2a2a2a] ml-[22px] my-0.5 pl-1.5">
          {node.children.map((child) => (
            <NodeRow
              key={child.key}
              node={child}
              depth={depth + 1}
              route={route}
              expanded={expanded}
              onToggle={onToggle}
              onGo={onGo}
            />
          ))}
        </ul>
      )}
    </li>
  );
};

const HubPanel: React.FC<NavHubProps> = ({
  onClose,
  route,
  onNavigate,
  user,
  quota,
  isAdmin = false,
  mfaEnrollmentRequired = false,
  reviewCount = 0,
  issuesOpenCount = 0,
  issuesUnreadCount = 0,
  onLogout,
  tier = 'all-in-one',
}) => {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const tree = buildNavTree({ isAdmin, mfaEnrollmentRequired, reviewCount, issuesOpenCount, issuesUnreadCount });
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set(activeAncestorKeys(tree, route)));

  // Focus moves in on open and returns to whatever opened the hub (the Menu key) on close.
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panelRef.current?.focus();
    return () => opener?.focus();
  }, []);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key !== 'Escape' || e.defaultPrevented) return;
      e.preventDefault();
      e.stopPropagation();
      onClose();
    };
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    window.addEventListener('keydown', onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [onClose]);

  const handleTrapTab = (e: React.KeyboardEvent<HTMLDivElement>): void => {
    if (e.key !== 'Tab' || !panelRef.current) return;
    const items = Array.from(panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === panelRef.current)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  const toggle = (key: string): void =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const go = (target: AppRoute): void => {
    onNavigate(target);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end md:justify-start" role="dialog" aria-modal="true" aria-label="Navigation menu">
      {/* Backdrop */}
      <div className="fixed inset-0 bg-black/80 md:bg-black/60 backdrop-blur-sm transition-opacity" onClick={onClose} aria-hidden="true" />

      {/* Hub panel: full-screen on phones, 320px side panel from md up */}
      <div
        ref={panelRef}
        tabIndex={-1}
        onKeyDown={handleTrapTab}
        className="relative w-full max-w-none md:w-80 md:max-w-[320px] bg-[#0c0c0c] border-l md:border-l-0 md:border-r border-[#222222] shadow-2xl flex flex-col h-full pt-safe pb-safe z-10 outline-none"
      >
        <div className="flex items-center justify-between px-3 py-2 border-b border-[#1f1f1f] bg-[#121212]">
          <div className="flex items-center gap-2.5">
            <img
              src="/trackseerr-logo.svg"
              alt="TrackSeerr"
              width={24} height={24} className="h-6 w-6 object-contain"
              onError={(e) => {
                e.currentTarget.src = '/static/trackseerr-logo.svg';
              }}
            />
            <div>
              <span className="text-sm font-black tracking-wider uppercase text-white font-mono leading-none">
                Track<span className="text-[#e5a00d]">Seerr</span>
                {tier === 'gateway' && ' Requests'}
                {tier === 'core' && (
                  <span className="ml-2 px-1.5 py-0.5 text-[9px] font-mono font-bold uppercase border border-[var(--border-default)] rounded-[3px] text-[var(--accent-amber)] align-middle">
                    Core
                  </span>
                )}
              </span>
              <p className="hidden md:block text-[10px] text-neutral-400 font-mono tracking-tight uppercase">Deck Controls</p>
            </div>
          </div>
          <TapeDeckButton size="sm" onClick={onClose} aria-label="Close menu" icon={<X className="h-4 w-4" />} />
        </div>

        {user && (
          <div className="px-3 py-2 bg-[#121212]/70 border-b border-[#1c1c1c] space-y-1.5">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 min-w-0">
                <div className="w-6 h-6 rounded-[3px] bg-[#1c1c1c] border border-[#2c2c2c] flex items-center justify-center flex-shrink-0">
                  <UserIcon className="h-3.5 w-3.5 text-[#e5a00d]" />
                </div>
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className="text-xs font-mono font-semibold text-white truncate">{user.plex_username}</span>
                    {user.is_admin && <Shield className="h-3 w-3 text-[#e5a00d] flex-shrink-0" />}
                  </div>
                  <span className="text-[10px] font-mono text-neutral-400">
                    {user.is_admin ? 'Deck Administrator' : 'Plex Member'}
                  </span>
                </div>
              </div>
            </div>
            <QuotaBadge quota={quota} className="w-full justify-between" />
          </div>
        )}

        <nav aria-label="Main navigation" className="flex-1 px-2 py-1.5 md:p-3 overflow-y-auto modal-body-scroll">
          <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-mono px-1 pt-0.5 pb-1">Navigation Deck</div>
          <ul className="flex flex-col gap-1">
            {tree.map((node) => (
              <NodeRow key={node.key} node={node} depth={0} route={route} expanded={expanded} onToggle={toggle} onGo={go} />
            ))}
          </ul>
        </nav>

        <div className="px-2 py-1.5 md:p-3 border-t border-[#1f1f1f] bg-[#0e0e0e] space-y-1">
          {user && onLogout && (
            <TapeDeckButton
              size="md"
              variant="default"
              onClick={() => {
                onClose();
                onLogout();
              }}
              icon={<LogOut className="h-4 w-4 text-red-400" />}
              className="w-full justify-center text-xs"
            >
              Sign Out
            </TapeDeckButton>
          )}
          <div className="text-center">
            <span className="text-[9px] leading-none uppercase tracking-widest text-neutral-600 font-mono">TrackSeerr Analog Deck v1.0</span>
          </div>
        </div>
      </div>
    </div>
  );
};

/** Hub-and-spoke navigator: every page of the app, reachable from any viewport. Mounted only while open. */
export const NavHub: React.FC<NavHubProps> = (props) => (props.isOpen ? <HubPanel {...props} /> : null);
