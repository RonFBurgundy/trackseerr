import React, { useState, useId } from 'react';
import { Loader2, Upload } from 'lucide-react';
import { ActionBar, TapeDeckButton } from '@/components/ui';
import { errorMessage } from '@/services/apiClient';

export interface M3uImportTabProps {
  onSubmit: (name: string, content: string) => Promise<void>;
}

const MAX_FILE_SIZE = 2 * 1024 * 1024; // 2 MB

export const M3uImportTab: React.FC<M3uImportTabProps> = ({ onSubmit }) => {
  const fileInputId = useId();
  const nameInputId = useId();

  const [name, setName] = useState<string>('');
  const [content, setContent] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setError(null);
    if (file.size > MAX_FILE_SIZE) {
      setError('File exceeds 2 MB limit');
      setContent('');
      return;
    }

    try {
      const text = await file.text();
      setContent(text);
      const baseName = file.name.replace(/\.[^/.]+$/, '');
      setName(baseName);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to read file'));
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!content) {
      setError('Please select an M3U file');
      return;
    }
    if (!name.trim()) {
      setError('Please enter a playlist name');
      return;
    }

    setIsSubmitting(true);
    setError(null);
    try {
      await onSubmit(name.trim(), content);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to import playlist'));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      {error && (
        <div
          role="alert"
          className="p-2.5 bg-red-950/40 border border-red-800/50 rounded-[3px] text-xs font-mono text-red-300"
        >
          {error}
        </div>
      )}

      <div>
        <label
          htmlFor={fileInputId}
          className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1"
        >
          M3U Playlist File (.m3u, .m3u8)
        </label>
        <input
          id={fileInputId}
          name="m3u-file"
          type="file"
          accept=".m3u,.m3u8,audio/x-mpegurl"
          onChange={handleFileChange}
          className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-neutral-300 file:mr-3 file:py-1 file:px-2.5 file:rounded-[3px] file:border file:border-[#383838] file:bg-[#1f1f1f] file:text-xs file:font-semibold file:uppercase file:tracking-wider file:text-white hover:file:text-[#e5a00d] file:cursor-pointer focus:outline-none focus:border-[#e5a00d]"
        />
        <p className="text-[11px] text-neutral-400 font-mono mt-1.5">
          Tracks are matched by the #EXTINF artist and title, or by file path against your library.
        </p>
      </div>

      <div>
        <label
          htmlFor={nameInputId}
          className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1"
        >
          Playlist Name
        </label>
        <input
          id={nameInputId}
          name="m3u-playlist-name"
          type="text"
          required
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            if (error) setError(null);
          }}
          placeholder="e.g. My Favorites"
          className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]"
        />
      </div>

      <ActionBar align="end" className="pt-2">
        <TapeDeckButton
          type="submit"
          variant="amber"
          size="md"
          disabled={isSubmitting || !content}
          icon={
            isSubmitting ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Upload className="h-4 w-4" />
            )
          }
        >
          {isSubmitting ? 'Importing...' : 'Import'}
        </TapeDeckButton>
      </ActionBar>
    </form>
  );
};
