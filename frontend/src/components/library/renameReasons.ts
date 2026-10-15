/** Human labels, explanations and risk class for the reason codes returned by the rename preview. */

export interface RenameReasonInfo {
  label: string;
  /** One-line explanation, shown as a tooltip. */
  help: string;
  /** Risky reasons change what a file or album is called, not just its tidiness. */
  risky: boolean;
}

export const RENAME_REASONS: Readonly<Record<string, RenameReasonInfo>> = {
  artist_folder: {
    label: 'Artist folder',
    help: 'The artist folder name differs from the artist folder format.',
    risky: false,
  },
  loose_file: {
    label: 'Loose file → album folder',
    help: 'The file sits outside an album folder and will be moved into one.',
    risky: false,
  },
  album_folder_format: {
    label: 'Album folder format',
    help: 'The album folder name differs from the naming format (spacing, casing, punctuation).',
    risky: false,
  },
  album_year: {
    label: 'Album year changed',
    help: 'The year in the album folder would change. Check the release date is right before applying.',
    risky: true,
  },
  album_title: {
    label: 'Album title changed',
    help: 'The album title in the path would change, not just its formatting.',
    risky: true,
  },
  disc_layout: {
    label: 'Disc layout',
    help: 'The disc folder or disc numbering would change.',
    risky: true,
  },
  track_number: {
    label: 'Track number changed',
    help: 'The track number in the file name would change. Often means tags and metadata disagree.',
    risky: true,
  },
  file_name_format: {
    label: 'File name format',
    help: 'The file name differs from the naming format only in formatting.',
    risky: false,
  },
  file_title: {
    label: 'Track title changed',
    help: 'The track title in the file name would change, not just its formatting.',
    risky: true,
  },
  collision: {
    label: 'Name collision',
    help: 'Another file already maps to this name; the move may be skipped or renamed.',
    risky: true,
  },
  verified: {
    label: 'Audio verified',
    help: 'AcoustID confirms this file is the track it is linked to.',
    risky: false,
  },
  audio_mismatch: {
    label: 'Audio is a different track',
    help: 'AcoustID identifies this audio as another track. Renaming is blocked until the file is linked to the right track.',
    risky: true,
  },
  compilation_link: {
    label: 'Compilation link',
    help: 'Moves a track between a Various Artists compilation and an artist folder — the track is probably linked to the wrong album; check it before applying.',
    risky: true,
  },
};

export function reasonInfo(code: string): RenameReasonInfo {
  return (
    RENAME_REASONS[code] ?? {
      label: code.replace(/_/g, ' '),
      help: 'Reason reported by the preview.',
      risky: false,
    }
  );
}
