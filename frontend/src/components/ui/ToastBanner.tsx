import React from 'react';
import { AlertTriangle, Check } from 'lucide-react';

export interface ToastBannerProps {
  message: string;
  tone: 'ok' | 'error';
}

export const ToastBanner: React.FC<ToastBannerProps> = ({ message, tone }) => (
  <div
    role="status"
    className={`fixed top-20 right-4 left-4 sm:left-auto z-50 bg-[#161616] border px-4 py-2.5 rounded-[4px] text-xs font-mono shadow-lg flex items-center gap-2 ${
      tone === 'error' ? 'border-red-700 text-red-300' : 'border-[#e5a00d] text-[#e5a00d]'
    }`}
  >
    {tone === 'error' ? <AlertTriangle className="h-4 w-4 shrink-0" /> : <Check className="h-4 w-4 shrink-0" />}
    <span>{message}</span>
  </div>
);
