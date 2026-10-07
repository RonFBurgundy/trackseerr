import React, { useEffect, useState } from 'react';
import { LOADING_MESSAGES, pickLoadingMessage } from '@/lib/loadingMessages';

export interface CassetteLoaderProps {
  size?: 'sm' | 'md' | 'lg';
  message?: string;
  className?: string;
}

const SIZES = {
  sm: { width: 64, height: 41 },
  md: { width: 96, height: 61 },
  lg: { width: 140, height: 90 },
} as const;

export const CassetteLoader: React.FC<CassetteLoaderProps> = ({
  size = 'md',
  message,
  className = '',
}) => {
  const [currentMessage, setCurrentMessage] = useState<string>(() => pickLoadingMessage());

  useEffect(() => {
    if (message !== undefined) {
      return;
    }

    const intervalId = window.setInterval(() => {
      setCurrentMessage((prev) => {
        if (LOADING_MESSAGES.length <= 1) {
          return prev;
        }
        let next = pickLoadingMessage();
        while (next === prev) {
          next = pickLoadingMessage();
        }
        return next;
      });
    }, 4000);

    return () => {
      window.clearInterval(intervalId);
    };
  }, [message]);

  const displayMessage = message ?? currentMessage;
  const dimensions = SIZES[size];

  return (
    <div
      role="status"
      aria-live="polite"
      className={`flex flex-col items-center justify-center gap-3 text-center ${className}`.trim()}
    >
      <svg
        width={dimensions.width}
        height={dimensions.height}
        viewBox="0 0 100 64"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden="true"
        className="shrink-0"
      >
        {/* Outer cassette shell */}
        <rect
          x="1"
          y="1"
          width="98"
          height="62"
          rx="4"
          ry="4"
          fill="#141414"
          stroke="#262626"
          strokeWidth="1.2"
        />

        {/* 4 Corner structural screws */}
        <circle cx="5" cy="5" r="1.2" fill="#222222" stroke="#333333" strokeWidth="0.4" />
        <circle cx="95" cy="5" r="1.2" fill="#222222" stroke="#333333" strokeWidth="0.4" />
        <circle cx="5" cy="59" r="1.2" fill="#222222" stroke="#333333" strokeWidth="0.4" />
        <circle cx="95" cy="59" r="1.2" fill="#222222" stroke="#333333" strokeWidth="0.4" />

        {/* Lower head bay trapezoid */}
        <polygon
          points="18,63 24,53 76,53 82,63"
          fill="#181818"
          stroke="#262626"
          strokeWidth="0.8"
        />
        {/* Roller / guide holes */}
        <circle cx="28" cy="58" r="2.2" fill="#0d0d0d" stroke="#262626" strokeWidth="0.5" />
        <circle cx="72" cy="58" r="2.2" fill="#0d0d0d" stroke="#262626" strokeWidth="0.5" />
        <circle cx="50" cy="59" r="1" fill="#222222" stroke="#333333" strokeWidth="0.3" />

        {/* Cassette label sticker */}
        <rect
          x="8"
          y="7"
          width="84"
          height="42"
          rx="3"
          ry="3"
          fill="#1c1c1c"
          stroke="#2a2a2a"
          strokeWidth="0.8"
        />

        {/* Label header: Side A + title write strip + accent stripe */}
        <text
          x="12"
          y="13"
          fill="#e5a00d"
          fontSize="4.5"
          fontFamily="monospace"
          fontWeight="bold"
        >
          A
        </text>
        <line x1="18" y1="12" x2="68" y2="12" stroke="#333333" strokeWidth="0.6" />
        <text
          x="88"
          y="13"
          fill="#666666"
          fontSize="3.2"
          fontFamily="monospace"
          fontWeight="bold"
          textAnchor="end"
        >
          C-90
        </text>

        {/* Amber brand accent stripes on label */}
        <path d="M 8 16 L 92 16" stroke="#e5a00d" strokeWidth="2" />
        <path d="M 8 18.5 L 92 18.5" stroke="#e5a00d" strokeWidth="0.6" strokeOpacity="0.4" />

        {/* Center cassette window */}
        <rect
          x="22"
          y="21"
          width="56"
          height="22"
          rx="3"
          ry="3"
          fill="#0a0a0a"
          stroke="#262626"
          strokeWidth="0.8"
        />
        <rect
          x="24"
          y="23"
          width="52"
          height="18"
          rx="2"
          ry="2"
          fill="#111111"
        />

        {/* Window tape progress ticks */}
        <line x1="46" y1="24.5" x2="46" y2="26.5" stroke="#333333" strokeWidth="0.5" />
        <line x1="50" y1="24.5" x2="50" y2="27.5" stroke="#4a4a4a" strokeWidth="0.6" />
        <line x1="54" y1="24.5" x2="54" y2="26.5" stroke="#333333" strokeWidth="0.5" />

        {/* Left tape pack (wound tape - slightly larger) */}
        <circle cx="35" cy="32" r="8.5" fill="#241a10" stroke="#3a2a1a" strokeWidth="0.6" />

        {/* Right tape pack (slightly smaller) */}
        <circle cx="65" cy="32" r="6.5" fill="#241a10" stroke="#3a2a1a" strokeWidth="0.6" />

        {/* Tape bridge at bottom */}
        <line x1="35" y1="40.5" x2="65" y2="38.5" stroke="#3a2a1a" strokeWidth="1" />

        {/* Rotating Left Spool */}
        <g className="cassette-spool">
          <circle cx="35" cy="32" r="4.8" fill="#e5e5e5" stroke="#ffffff" strokeWidth="0.3" />
          {/* 6 radial spokes / tooth slits */}
          <line x1="30.4" y1="32" x2="39.6" y2="32" stroke="#141414" strokeWidth="0.8" />
          <line x1="32.7" y1="28.02" x2="37.3" y2="35.98" stroke="#141414" strokeWidth="0.8" />
          <line x1="32.7" y1="35.98" x2="37.3" y2="28.02" stroke="#141414" strokeWidth="0.8" />
          {/* Spindle hole */}
          <circle cx="35" cy="32" r="2.2" fill="#0a0a0a" stroke="#262626" strokeWidth="0.3" />
          {/* 6 drive teeth in hole */}
          <line x1="33.5" y1="32" x2="36.5" y2="32" stroke="#e5e5e5" strokeWidth="0.6" />
          <line x1="34.25" y1="30.7" x2="35.75" y2="33.3" stroke="#e5e5e5" strokeWidth="0.6" />
          <line x1="34.25" y1="33.3" x2="35.75" y2="30.7" stroke="#e5e5e5" strokeWidth="0.6" />
          {/* Amber hub center jewel */}
          <circle cx="35" cy="32" r="1" fill="#e5a00d" />
        </g>

        {/* Rotating Right Spool */}
        <g className="cassette-spool">
          <circle cx="65" cy="32" r="4.8" fill="#e5e5e5" stroke="#ffffff" strokeWidth="0.3" />
          {/* 6 radial spokes / tooth slits */}
          <line x1="60.4" y1="32" x2="69.6" y2="32" stroke="#141414" strokeWidth="0.8" />
          <line x1="62.7" y1="28.02" x2="67.3" y2="35.98" stroke="#141414" strokeWidth="0.8" />
          <line x1="62.7" y1="35.98" x2="67.3" y2="28.02" stroke="#141414" strokeWidth="0.8" />
          {/* Spindle hole */}
          <circle cx="65" cy="32" r="2.2" fill="#0a0a0a" stroke="#262626" strokeWidth="0.3" />
          {/* 6 drive teeth in hole */}
          <line x1="63.5" y1="32" x2="66.5" y2="32" stroke="#e5e5e5" strokeWidth="0.6" />
          <line x1="64.25" y1="30.7" x2="65.75" y2="33.3" stroke="#e5e5e5" strokeWidth="0.6" />
          <line x1="64.25" y1="33.3" x2="65.75" y2="30.7" stroke="#e5e5e5" strokeWidth="0.6" />
          {/* Amber hub center jewel */}
          <circle cx="65" cy="32" r="1" fill="#e5a00d" />
        </g>
      </svg>
      <span className="text-xs uppercase tracking-widest text-neutral-400 font-mono select-none">
        {displayMessage}
      </span>
    </div>
  );
};
