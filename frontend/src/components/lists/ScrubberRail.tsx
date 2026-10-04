import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { GroupIndexGroup } from '@/types/activity';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import { useListViewport, type ListViewport } from './ListViewportContext';

export interface ScrubberRailProps {
  /** Group index for the list (`useGroupIndex`). Only these groups are shown. */
  groups: readonly GroupIndexGroup[];
  /**
   * With no groups, render a thin draggable position thumb instead of hiding. Default false: no groups, no rail.
   */
  positionOnly?: boolean;
  ariaLabel?: string;
}

/** Index of the group containing `index` (the last group whose offset is <= index), by binary search. */
export function groupIndexForOffset(groups: readonly GroupIndexGroup[], index: number): number {
  let lo = 0;
  let hi = groups.length - 1;
  let found = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (groups[mid].offset <= index) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found;
}

const DESKTOP_WIDTH_PX = 28;
/**
 * Overlay rail (narrow screens): drawn over the list's right edge. 24px holds a 3-char 9px bold mono label
 * (about 16px) plus OVERLAY_LABEL_PAD_PX at rest. Published to the scroll element as `--overlay-rail-w` so the
 * list's right padding (index.css) is derived from this one constant.
 */
const OVERLAY_WIDTH_PX = 24;
const OVERLAY_LABEL_PAD_PX = 3;
const MIN_LABEL_GAP_PX = 16;
const MIN_LABEL_GAP_TOUCH_PX = 14;
const DOT_GAP_PX = 5;
/** Fisheye: how far (px) the magnifier reaches, and how much the label under the pointer grows. */
const FISHEYE_SIGMA_PX = 34;
const FISHEYE_GAIN = 0.75;
const KEYBOARD_BALLOON_MS = 900;
const TOUCH_BALLOON_LINGER_MS = 350;
const DRAG_THRESHOLD_PX = 3;
const PAGE_STEP = 5;

interface RailItem {
  index: number;
  kind: 'label' | 'dot';
}

/** Chooses which groups get a label and which collapse to a dot; first and last always keep a label. */
export function layoutRailItems(count: number, heightPx: number, minLabelGapPx: number): RailItem[] {
  if (count <= 0 || heightPx <= 0) return [];
  const slot = heightPx / count;
  const labelStride = Math.max(1, Math.ceil(minLabelGapPx / slot));
  const dotStride = Math.max(1, Math.ceil(DOT_GAP_PX / slot));
  const items: RailItem[] = [];
  for (let i = 0; i < count; i += 1) {
    const isLast = i === count - 1;
    if (i === 0 || isLast) {
      items.push({ index: i, kind: 'label' });
    } else if (i % labelStride === 0 && count - 1 - i >= labelStride) {
      items.push({ index: i, kind: 'label' });
    } else if (labelStride > 1 && i % dotStride === 0) {
      items.push({ index: i, kind: 'dot' });
    }
  }
  return items;
}

/**
 * Short form of a group label for the slim overlay rail (about 18px wide): `2010` -> `'10`, `Sep 2026` -> `Sep`, and
 * anything else its first three characters. The balloon always shows the full label.
 */
export function overlayLabel(label: string): string {
  if (/^\d{4}$/.test(label)) return `'${label.slice(2)}`;
  return label.length > 3 ? label.slice(0, 3).trimEnd() : label;
}

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
    : false;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function haptic(): void {
  if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') {
    navigator.vibrate(6);
  }
}

interface RailProps {
  groups: readonly GroupIndexGroup[];
  viewport: ListViewport;
  /** Coarse pointer: no fisheye, haptics, longer balloon linger. */
  touch: boolean;
  ariaLabel: string;
}

const BALLOON_STYLE: React.CSSProperties = {
  background: 'linear-gradient(180deg, #1f1f1f 0%, #151515 100%)',
  border: '1px solid #2a2a2a',
  borderTopColor: '#383838',
  borderBottomColor: '#111111',
  boxShadow: '0 2px 0 #050505, 0 8px 20px rgba(0,0,0,0.65), inset 0 1px 0 rgba(255,255,255,0.08)',
};

const GroupRail: React.FC<RailProps> = ({ groups, viewport, touch, ariaLabel }) => {
  const { topIndex, atEnd, overlayRail, scrollElement, total, scrollToOffset } = viewport;
  const count = groups.length;
  const trackRef = useRef<HTMLDivElement | null>(null);
  const balloonRef = useRef<HTMLDivElement | null>(null);
  const labelRefs = useRef<Map<number, HTMLSpanElement>>(new Map());
  const [height, setHeight] = useState<number>(0);
  /** Group under the pointer (hover or drag); null when idle. */
  const [pointerIndex, setPointerIndex] = useState<number | null>(null);
  const [keyboardIndex, setKeyboardIndex] = useState<number | null>(null);
  /**
   * Group the user last jumped to (key, click, drag release). It is the active group until the user scrolls the list
   * by hand: at the end of a short last group the list cannot reach a group's first row, so the scroll position alone
   * would keep reporting an earlier group.
   */
  const [pinnedIndex, setPinnedIndex] = useState<number | null>(null);
  const [dragging, setDragging] = useState<boolean>(false);
  const dragRef = useRef<{ active: boolean; startY: number; moved: boolean; pointerId: number }>({
    active: false,
    startY: 0,
    moved: false,
    pointerId: -1,
  });
  const latestYRef = useRef<number>(0);
  const lastOffsetRef = useRef<number>(-1);
  const rafRef = useRef<number | null>(null);
  const lingerTimerRef = useRef<number | null>(null);
  const kbdTimerRef = useRef<number | null>(null);
  const lastIndexRef = useRef<number>(-1);
  const hoveringRef = useRef<boolean>(false);

  useEffect(() => {
    const el = trackRef.current;
    if (!el) return undefined;
    const update = (): void => setHeight(el.clientHeight);
    update();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(
    () => () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      if (lingerTimerRef.current !== null) window.clearTimeout(lingerTimerRef.current);
      if (kbdTimerRef.current !== null) window.clearTimeout(kbdTimerRef.current);
    },
    []
  );

  const items = useMemo(
    () => layoutRailItems(count, height, touch ? MIN_LABEL_GAP_TOUCH_PX : MIN_LABEL_GAP_PX),
    [count, height, touch]
  );

  const slot = count > 0 ? height / count : 0;
  const slotRef = useRef<number>(slot);
  slotRef.current = slot;

  // Scrolled to the very end: the active group is the last one, whatever row is at the top.
  const scrollIndex = useMemo(
    () => (atEnd ? count - 1 : groupIndexForOffset(groups, topIndex)),
    [atEnd, count, groups, topIndex]
  );
  const activeIndex = clamp(pinnedIndex ?? scrollIndex, 0, Math.max(0, count - 1));
  const shownIndex = pointerIndex ?? keyboardIndex ?? null;
  const markerIndex = pointerIndex ?? activeIndex;

  // New groups (sort, filter, reload) invalidate a pinned group.
  useEffect(() => {
    setPinnedIndex(null);
  }, [groups]);

  // Scrolling the list by hand (wheel, touch, its own keys, scrollbar) hands the marker back to the scroll position.
  useEffect(() => {
    if (!scrollElement) return undefined;
    const release = (): void => setPinnedIndex(null);
    const events: ReadonlyArray<keyof HTMLElementEventMap> = ['wheel', 'touchstart', 'pointerdown', 'keydown'];
    for (const name of events) scrollElement.addEventListener(name, release, { passive: true });
    return () => {
      for (const name of events) scrollElement.removeEventListener(name, release);
    };
  }, [scrollElement]);
  const balloonIndex = shownIndex;

  const applyFisheye = useCallback(
    (y: number | null): void => {
      const flat = y === null || touch || prefersReducedMotion();
      for (const [i, el] of labelRefs.current) {
        if (flat) {
          el.style.transform = '';
          el.style.opacity = '';
          continue;
        }
        const d = Math.abs((i + 0.5) * slotRef.current - y);
        const g = Math.exp(-((d / FISHEYE_SIGMA_PX) ** 2));
        const s = 1 + FISHEYE_GAIN * g;
        el.style.transform = `translateX(${(-(s - 1) * 7).toFixed(2)}px) scale(${s.toFixed(3)})`;
        el.style.opacity = String((0.55 + 0.45 * g).toFixed(3));
      }
    },
    [touch]
  );

  /** One rAF per frame: read the track rect once, then write transforms only (no layout thrash). */
  const frame = useCallback((): void => {
    rafRef.current = null;
    const track = trackRef.current;
    if (!track || count === 0) return;
    const rect = track.getBoundingClientRect();
    const y = clamp(latestYRef.current - rect.top, 0, rect.height);
    const f = rect.height > 0 ? (y / rect.height) * count : 0;
    const i = clamp(Math.floor(f), 0, count - 1);
    const frac = clamp(f - i, 0, 0.999);

    const balloon = balloonRef.current;
    if (balloon) balloon.style.transform = `translateY(${(y - balloon.offsetHeight / 2).toFixed(1)}px)`;
    applyFisheye(y);

    if (lastIndexRef.current !== i) {
      lastIndexRef.current = i;
      setPointerIndex(i);
      if (dragRef.current.active && touch) haptic();
    }
    if (dragRef.current.active) {
      const group = groups[i];
      const exact = !dragRef.current.moved;
      const offset = exact
        ? group.offset
        : clamp(group.offset + Math.floor(frac * group.count), 0, Math.max(0, total - 1));
      if (offset !== lastOffsetRef.current) {
        lastOffsetRef.current = offset;
        scrollToOffset(offset);
      }
    }
  }, [applyFisheye, count, groups, scrollToOffset, total, touch]);

  const schedule = useCallback((clientY: number): void => {
    latestYRef.current = clientY;
    if (rafRef.current === null) rafRef.current = requestAnimationFrame(frame);
  }, [frame]);

  const endInteraction = useCallback((): void => {
    dragRef.current.active = false;
    setDragging(false);
    lastOffsetRef.current = -1;
    const clear = (): void => {
      lastIndexRef.current = -1;
      hoveringRef.current = false;
      setPointerIndex(null);
      applyFisheye(null);
    };
    if (touch) {
      if (lingerTimerRef.current !== null) window.clearTimeout(lingerTimerRef.current);
      lingerTimerRef.current = window.setTimeout(clear, TOUCH_BALLOON_LINGER_MS);
    } else if (!hoveringRef.current) {
      clear();
    }
  }, [applyFisheye, touch]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>): void => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (count === 0) return;
    if (lingerTimerRef.current !== null) window.clearTimeout(lingerTimerRef.current);
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = { active: true, startY: e.clientY, moved: false, pointerId: e.pointerId };
    setDragging(true);
    lastIndexRef.current = -1;
    lastOffsetRef.current = -1;
    // Jump now, not on the next frame: a short tap releases before any frame would run.
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    latestYRef.current = e.clientY;
    frame();
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>): void => {
    if (dragRef.current.active) {
      if (!dragRef.current.moved && Math.abs(e.clientY - dragRef.current.startY) >= DRAG_THRESHOLD_PX) {
        dragRef.current.moved = true;
      }
      schedule(e.clientY);
      return;
    }
    if (e.pointerType === 'mouse') {
      hoveringRef.current = true;
      schedule(e.clientY);
    }
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>): void => {
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
    if (e.type === 'pointerup' && dragRef.current.active) {
      // A tap (no drag): make sure the jump for the group under the finger happened, synchronously.
      if (!dragRef.current.moved) {
        latestYRef.current = e.clientY;
        frame();
      }
      if (lastIndexRef.current >= 0) setPinnedIndex(lastIndexRef.current);
    }
    endInteraction();
  };

  const onPointerLeave = (): void => {
    hoveringRef.current = false;
    if (!dragRef.current.active) {
      lastIndexRef.current = -1;
      setPointerIndex(null);
      applyFisheye(null);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>): void => {
    let next = activeIndex;
    switch (e.key) {
      case 'ArrowUp':
      case 'ArrowLeft':
        next = activeIndex - 1;
        break;
      case 'ArrowDown':
      case 'ArrowRight':
        next = activeIndex + 1;
        break;
      case 'PageUp':
        next = activeIndex - PAGE_STEP;
        break;
      case 'PageDown':
        next = activeIndex + PAGE_STEP;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = count - 1;
        break;
      case 'Enter':
      case ' ':
        next = activeIndex;
        break;
      default:
        return;
    }
    e.preventDefault();
    next = clamp(next, 0, count - 1);
    // The target group is the active group straight away; the scroll position follows it.
    setPinnedIndex(next);
    scrollToOffset(groups[next].offset);
    setKeyboardIndex(next);
    if (kbdTimerRef.current !== null) window.clearTimeout(kbdTimerRef.current);
    kbdTimerRef.current = window.setTimeout(() => setKeyboardIndex(null), KEYBOARD_BALLOON_MS);
  };

  const width = overlayRail ? OVERLAY_WIDTH_PX : DESKTOP_WIDTH_PX;
  const markerHeight = clamp(slot, 6, 22);
  const markerY = (markerIndex + 0.5) * slot - markerHeight / 2;
  const labelText = overlayRail ? 'text-[9px]' : 'text-[10px]';

  return (
    <div className="relative h-full" style={{ width }}>
      <div
        role="slider"
        tabIndex={0}
        aria-label={ariaLabel}
        aria-orientation="vertical"
        aria-valuemin={0}
        aria-valuemax={Math.max(0, count - 1)}
        aria-valuenow={activeIndex}
        aria-valuetext={groups[activeIndex]?.label ?? ''}
        onKeyDown={onKeyDown}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onPointerLeave={onPointerLeave}
        onFocus={() => setKeyboardIndex(null)}
        className={`scrubber-rail h-full select-none touch-none cursor-pointer py-1.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[#e5a00d] ${
          overlayRail ? 'rounded-r-[3px]' : 'rounded-[4px] border border-[#1a1a1a] bg-[#0d0d0d]'
        } ${dragging ? 'cursor-grabbing' : ''}`}
        style={
          overlayRail
            ? { background: 'linear-gradient(to left, rgba(10,10,10,0.6), rgba(10,10,10,0))' }
            : { boxShadow: 'inset 0 2px 4px rgba(0,0,0,0.8), 0 1px 0 rgba(255,255,255,0.04)' }
        }
      >
        <div ref={trackRef} className="relative h-full">
          {overlayRail && (
            <div aria-hidden="true" className="absolute inset-y-0 right-[3px] w-px bg-[#2a2a2a]" />
          )}
          {height > 0 && (
            <div
              aria-hidden="true"
              className="scrubber-marker absolute left-0 w-[2px] bg-[#e5a00d]"
              style={{
                height: markerHeight,
                transform: `translateY(${markerY.toFixed(1)}px)`,
                boxShadow: '0 0 6px rgba(229,160,13,0.8)',
              }}
            />
          )}
          {items.map((item) => {
            const y = (item.index + 0.5) * slot;
            const active = item.index === markerIndex;
            if (item.kind === 'dot') {
              return (
                <span
                  key={item.index}
                  aria-hidden="true"
                  className={`absolute ${overlayRail ? 'right-[2px]' : 'right-[9px]'} h-[3px] w-[3px] rounded-full ${active ? 'bg-[#e5a00d]' : 'bg-[#444444]'}`}
                  style={{ top: y - 1.5 }}
                />
              );
            }
            return (
              <div
                key={item.index}
                aria-hidden="true"
                className={`absolute right-0 left-0 flex items-center justify-end ${overlayRail ? '' : 'pr-1.5'}`}
                style={{ top: y, ...(overlayRail ? { paddingRight: OVERLAY_LABEL_PAD_PX } : {}), transform: 'translateY(-50%)', height: 0 }}
              >
                <span
                  ref={(el) => {
                    if (el) labelRefs.current.set(item.index, el);
                    else labelRefs.current.delete(item.index);
                  }}
                  className={`scrubber-label origin-right font-mono font-bold leading-none whitespace-nowrap ${labelText} ${
                    active ? 'text-[#e5a00d]' : 'text-neutral-400'
                  }`}
                >
                  {overlayRail ? overlayLabel(groups[item.index].label) : groups[item.index].label}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* Balloon: the active label in a tape-deck badge, offset to the left of the rail (further on touch). */}
      <div
        ref={balloonRef}
        aria-hidden="true"
        className="scrubber-balloon pointer-events-none absolute top-0 z-30"
        style={{
          right: width + (overlayRail ? 20 : 12),
          // Pointer interaction positions the balloon imperatively (rAF); the keyboard path parks it at the marker.
          transform: pointerIndex === null ? `translateY(${(markerY + markerHeight / 2 - 22).toFixed(1)}px)` : undefined,
          opacity: balloonIndex === null ? 0 : 1,
          visibility: balloonIndex === null ? 'hidden' : 'visible',
        }}
      >
        <div
          className="relative rounded-[4px] px-3.5 py-2 min-w-[44px] text-center font-mono font-bold text-[22px] leading-none text-[#e5a00d] whitespace-nowrap"
          style={{ ...BALLOON_STYLE, textShadow: '0 0 8px rgba(229,160,13,0.35)' }}
        >
          <span className="absolute inset-x-0 top-0 h-[2px] bg-[#e5a00d]" style={{ boxShadow: '0 0 6px rgba(229,160,13,0.8)' }} />
          {balloonIndex !== null ? groups[balloonIndex]?.label : ''}
          <span
            className="absolute top-1/2 -right-[5px] h-2.5 w-2.5 -translate-y-1/2 rotate-45 border-r border-t border-[#2a2a2a] bg-[#171717]"
          />
        </div>
      </div>
    </div>
  );
};

const PositionRail: React.FC<{ viewport: ListViewport; ariaLabel: string }> = ({ viewport, ariaLabel }) => {
  const { scrollElement } = viewport;
  const trackRef = useRef<HTMLDivElement | null>(null);
  const thumbRef = useRef<HTMLDivElement | null>(null);
  const draggingRef = useRef<boolean>(false);
  const [percent, setPercent] = useState<number>(0);

  const layout = useCallback((): void => {
    const track = trackRef.current;
    const thumb = thumbRef.current;
    if (!track || !thumb || !scrollElement) return;
    const trackH = track.clientHeight;
    const scrollable = Math.max(1, scrollElement.scrollHeight - scrollElement.clientHeight);
    const thumbH = clamp((scrollElement.clientHeight / scrollElement.scrollHeight) * trackH, 28, trackH);
    const f = clamp(scrollElement.scrollTop / scrollable, 0, 1);
    thumb.style.height = `${thumbH}px`;
    thumb.style.transform = `translateY(${(f * (trackH - thumbH)).toFixed(1)}px)`;
    setPercent((prev) => (Math.round(f * 100) === prev ? prev : Math.round(f * 100)));
  }, [scrollElement]);

  useEffect(() => {
    if (!scrollElement) return undefined;
    let raf: number | null = null;
    const onScroll = (): void => {
      if (raf === null) {
        raf = requestAnimationFrame(() => {
          raf = null;
          layout();
        });
      }
    };
    layout();
    scrollElement.addEventListener('scroll', onScroll, { passive: true });
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(onScroll) : null;
    observer?.observe(scrollElement);
    return () => {
      scrollElement.removeEventListener('scroll', onScroll);
      observer?.disconnect();
      if (raf !== null) cancelAnimationFrame(raf);
    };
  }, [scrollElement, layout, viewport.total]);

  const scrubTo = (clientY: number): void => {
    const track = trackRef.current;
    if (!track || !scrollElement) return;
    const rect = track.getBoundingClientRect();
    const f = clamp((clientY - rect.top) / Math.max(1, rect.height), 0, 1);
    scrollElement.scrollTop = f * (scrollElement.scrollHeight - scrollElement.clientHeight);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>): void => {
    if (!scrollElement) return;
    const page = scrollElement.clientHeight * 0.9;
    if (e.key === 'ArrowUp') scrollElement.scrollTop -= 48;
    else if (e.key === 'ArrowDown') scrollElement.scrollTop += 48;
    else if (e.key === 'PageUp') scrollElement.scrollTop -= page;
    else if (e.key === 'PageDown') scrollElement.scrollTop += page;
    else if (e.key === 'Home') scrollElement.scrollTop = 0;
    else if (e.key === 'End') scrollElement.scrollTop = scrollElement.scrollHeight;
    else return;
    e.preventDefault();
  };

  return (
    <div
      role="slider"
      tabIndex={0}
      aria-label={ariaLabel}
      aria-orientation="vertical"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
      aria-valuetext={`${percent} percent`}
      onKeyDown={onKeyDown}
      onPointerDown={(e) => {
        e.currentTarget.setPointerCapture(e.pointerId);
        draggingRef.current = true;
        scrubTo(e.clientY);
      }}
      onPointerMove={(e) => {
        if (draggingRef.current) scrubTo(e.clientY);
      }}
      onPointerUp={(e) => {
        draggingRef.current = false;
        if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
      }}
      onPointerCancel={() => {
        draggingRef.current = false;
      }}
      className="h-full touch-none select-none cursor-pointer rounded-[4px] border border-[#1f1f1f] bg-[#0d0d0d] py-1.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#e5a00d]"
      style={{
        width: viewport.overlayRail ? OVERLAY_WIDTH_PX : 14,
        boxShadow: 'inset 0 2px 4px rgba(0,0,0,0.8), 0 1px 0 rgba(255,255,255,0.04)',
      }}
    >
      <div ref={trackRef} className="relative h-full">
        <div
          ref={thumbRef}
          className="absolute inset-x-0.5 rounded-[2px] border border-[#383838] bg-gradient-to-b from-[#2a2a2a] to-[#1c1c1c]"
          style={{ boxShadow: '0 1px 3px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.08)' }}
        />
      </div>
    </div>
  );
};

/**
 * Right-side scrubber for virtualized lists. Mount it in the `rail` slot of FlatList / VirtualGrid; it reads the
 * list's viewport from context. It hides when the content fits or there are fewer than two groups, and while shown it
 * hides the list's native scrollbar (wheel, keyboard and touch scrolling keep working).
 */
export const ScrubberRail: React.FC<ScrubberRailProps> = ({ groups, positionOnly = false, ariaLabel = 'Jump to group' }) => {
  const viewport = useListViewport();
  const touch = useMediaQuery('(pointer: coarse)');
  const groupMode = groups.length >= 2;
  const visible = viewport !== null && viewport.scrollElement !== null && !viewport.fits && (groupMode || positionOnly);
  const scrollElement = viewport?.scrollElement ?? null;

  const overlay = viewport?.overlayRail ?? false;
  useEffect(() => {
    if (!scrollElement || !visible) return undefined;
    scrollElement.dataset.scrubber = 'on';
    if (overlay) {
      scrollElement.dataset.rail = 'overlay';
      scrollElement.style.setProperty('--overlay-rail-w', `${OVERLAY_WIDTH_PX}px`);
    }
    return () => {
      scrollElement.style.removeProperty('--overlay-rail-w');
      delete scrollElement.dataset.scrubber;
      delete scrollElement.dataset.rail;
    };
  }, [scrollElement, visible, overlay]);

  if (!viewport || !visible) return null;
  return groupMode ? (
    <GroupRail groups={groups} viewport={viewport} touch={touch} ariaLabel={ariaLabel} />
  ) : (
    <PositionRail viewport={viewport} ariaLabel={ariaLabel} />
  );
};
