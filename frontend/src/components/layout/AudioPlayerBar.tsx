import React, { useEffect, useRef } from 'react';
import { Play, Pause, Square, Music } from 'lucide-react';
import type { AudioPreviewTrack } from '@/types/models';
import { TapeDeckButton } from '@/components/ui';

export interface AudioPlayerBarProps {
  currentTrack: AudioPreviewTrack | null;
  isPlaying: boolean;
  progress: number;
  duration: number;
  currentTime: number;
  onToggle: (track: AudioPreviewTrack) => void;
  onStop: () => void;
  onSeek: (fraction: number) => void;
}

export const AudioPlayerBar: React.FC<AudioPlayerBarProps> = ({
  currentTrack,
  isPlaying,
  progress,
  duration,
  currentTime,
  onToggle,
  onStop,
  onSeek,
}) => {
  const barRef = useRef<HTMLDivElement>(null);
  const visible = currentTrack !== null;

  // Publish the bar height so floating sheets (bulk editors) can sit above it.
  useEffect(() => {
    const root = document.documentElement;
    const el = barRef.current;
    if (!visible || !el) {
      root.style.removeProperty('--player-offset');
      return undefined;
    }
    const publish = (): void => root.style.setProperty('--player-offset', `${el.offsetHeight}px`);
    publish();
    const observer = new ResizeObserver(publish);
    observer.observe(el);
    return () => {
      observer.disconnect();
      root.style.removeProperty('--player-offset');
    };
  }, [visible]);

  if (!currentTrack) return null;

  const handleScrubberClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const fraction = Math.max(0, Math.min(1, clickX / rect.width));
    onSeek(fraction);
  };

  const formatTime = (secs: number) => {
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    return `${m}:${s < 10 ? '0' : ''}${s}`;
  };

  return (
    <div ref={barRef} className="fixed bottom-0 left-0 right-0 z-40 bg-[#0d0d0d] border-t border-[#1f1f1f] shadow-[0_-4px_16px_rgba(0,0,0,0.8)] pb-safe">
      {/* 30s Scrubber Progress Bar */}
      <div
        className="w-full h-1.5 bg-[#181818] cursor-pointer relative group"
        onClick={handleScrubberClick}
        title="Seek preview"
      >
        <div
          className="h-full bg-[#e5a00d] shadow-[0_0_6px_rgba(229,160,13,0.8)] transition-all duration-75 relative"
          style={{ width: `${progress}%` }}
        >
          <div className="absolute right-0 top-1/2 -translate-y-1/2 h-3 w-3 bg-white rounded-full opacity-0 group-hover:opacity-100 transition-opacity" />
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-2.5 flex items-center justify-between gap-4">
        {/* Track Metadata */}
        <div className="flex items-center gap-3 min-w-0 flex-1">
          {currentTrack.cover_url ? (
            <img
              src={currentTrack.cover_url}
              alt=""
              className="h-10 w-10 sm:h-12 sm:w-12 rounded-[3px] object-cover border border-[#222222] shrink-0"
              onError={(e) => {
                (e.currentTarget as HTMLImageElement).src = '/placeholder.svg';
              }}
            />
          ) : (
            <div className="h-10 w-10 sm:h-12 sm:w-12 rounded-[3px] bg-[#1a1a1a] border border-[#222222] flex items-center justify-center shrink-0">
              <Music className="h-5 w-5 text-neutral-500" />
            </div>
          )}

          <div className="min-w-0 flex-1">
            <h4 className="text-xs sm:text-sm font-bold text-white truncate">
              {currentTrack.title}
            </h4>
            <p className="text-[11px] sm:text-xs text-neutral-400 truncate">
              {currentTrack.artist}
            </p>
          </div>
        </div>

        {/* Transport Controls */}
        <div className="flex items-center gap-2 shrink-0">
          <span className="hidden sm:inline font-mono text-xs text-neutral-400">
            {formatTime(currentTime)} / {formatTime(duration || 30)}
          </span>

          <TapeDeckButton
            size="md"
            active={isPlaying}
            onClick={() => onToggle(currentTrack)}
            aria-label={isPlaying ? 'Pause preview' : 'Play preview'}
            icon={
              isPlaying ? (
                <Pause className="h-4 w-4 text-[#e5a00d]" />
              ) : (
                <Play className="h-4 w-4 fill-white" />
              )
            }
          />

          <TapeDeckButton
            size="md"
            onClick={onStop}
            aria-label="Stop preview"
            icon={<Square className="h-4 w-4" />}
          />
        </div>
      </div>
    </div>
  );
};
