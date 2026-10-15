import { useCallback, useState } from 'react';
import type { AlbumItem } from '@/types/models';
import { getMbReleaseSeed, linkMbRelease } from '@/services/libraryService';
import { postSeedForm } from '@/utils';
import { errorMessage } from '@/services/apiClient';

export interface UseMbSubmitReturn {
  isSubmitting: boolean;
  submitError: string | null;
  submit: (albumId: string | number) => Promise<void>;
  isLinking: boolean;
  linkError: string | null;
  link: (albumId: string | number, mbidOrUrl: string) => Promise<AlbumItem>;
}

export function useMbSubmit(): UseMbSubmitReturn {
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [isLinking, setIsLinking] = useState<boolean>(false);
  const [linkError, setLinkError] = useState<string | null>(null);

  const submit = useCallback(async (albumId: string | number): Promise<void> => {
    setIsSubmitting(true);
    setSubmitError(null);
    try {
      const redirectUri = `${window.location.origin}${window.location.pathname}?mb_album=${encodeURIComponent(String(albumId))}`;
      const seed = await getMbReleaseSeed(albumId, redirectUri);
      postSeedForm(seed.action_url, seed.fields);
    } catch (err: unknown) {
      const msg = errorMessage(err, 'Failed to submit to MusicBrainz');
      setSubmitError(msg);
      throw err;
    } finally {
      setIsSubmitting(false);
    }
  }, []);

  const link = useCallback(async (albumId: string | number, mbidOrUrl: string): Promise<AlbumItem> => {
    setIsLinking(true);
    setLinkError(null);
    try {
      return await linkMbRelease(albumId, mbidOrUrl.trim());
    } catch (err: unknown) {
      const msg = errorMessage(err, 'Failed to link MusicBrainz release');
      setLinkError(msg);
      throw err;
    } finally {
      setIsLinking(false);
    }
  }, []);

  return {
    isSubmitting,
    submitError,
    submit,
    isLinking,
    linkError,
    link,
  };
}
