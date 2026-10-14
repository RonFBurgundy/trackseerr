// Lint script against stray blue / indigo / sky Tailwind classes and unauthorized raw native inputs.
//   node scripts/check-ui-guard.mjs
// Walks frontend/src (*.ts, *.tsx, *.css) and fails if:
// (a) any Tailwind colour class matching blue/indigo/sky is found
// (b) type="checkbox", type="radio", or type="range" is in a .tsx file outside src/components/ui/ unless allowlisted
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const srcDir = join(frontendDir, 'src');

// Frozen debt: do not add to this list; use SelectionCheckbox / TactileSwitch / the ui slider
const ALLOWLIST = new Set([
  'src/components/account/RecoveryCodesModal.tsx',
  'src/components/activity/ActivityQueuePanel.tsx',
  'src/components/admin/CreateUserModal.tsx',
  'src/components/admin/UserEditModal.tsx',
  'src/components/library/ArtistBulkBar.tsx',
  'src/components/library/RenameModal.tsx',
  'src/components/library/RetagModal.tsx',
  'src/components/lists/FlatList.tsx',
  'src/components/manualImport/ManualImportRowView.tsx',
  'src/components/mixes/MixCard.tsx',
  'src/components/notifications/NotificationChannelModal.tsx',
  'src/components/notifications/UserChannelModal.tsx',
  'src/components/playlists/SmartCollectionModal.tsx',
  'src/components/profiles/CustomFormatSpecRow.tsx',
  'src/components/profiles/KbpsSlider.tsx',
  'src/components/profiles/MetadataProfileEditorModal.tsx',
  'src/components/profiles/QualityEntryList.tsx',
  'src/components/profiles/ReleaseProfileEditorModal.tsx',
  'src/components/settings/ItunesImportCard.tsx',
  'src/components/system/SystemLogsPanel.tsx',
  'src/components/views/CalendarView.tsx',
]);

const COLOR_REGEX = /\b(?:[a-z]+:)*(?:bg|text|border|ring|from|via|to|fill|stroke|accent|outline|decoration|shadow|divide|placeholder|caret)-(?:blue|indigo|sky)-\d{2,3}\b/;
const INPUT_REGEX = /type=["'](checkbox|radio|range)["']/;

function walkDir(dir) {
  let files = [];
  const entries = readdirSync(dir, { withFileTypes: true });
  for (const entry of entries) {
    const fullPath = join(dir, entry.name);
    if (entry.isDirectory()) {
      files = files.concat(walkDir(fullPath));
    } else if (entry.isFile()) {
      const ext = entry.name.slice(entry.name.lastIndexOf('.'));
      if (['.ts', '.tsx', '.css'].includes(ext)) {
        files.push(fullPath);
      }
    }
  }
  return files;
}

const allFiles = walkDir(srcDir);
const errors = [];

for (const file of allFiles) {
  const relPath = relative(frontendDir, file).replace(/\\/g, '/');
  const isTsx = file.endsWith('.tsx');
  const isInsideUi = relPath.startsWith('src/components/ui/');
  const isAllowlisted = ALLOWLIST.has(relPath);

  const content = readFileSync(file, 'utf8');
  const lines = content.split('\n');

  for (let i = 0; i < lines.length; i++) {
    const lineNum = i + 1;
    const line = lines[i];

    // Check (a): Tailwind colour classes
    const colorMatch = line.match(COLOR_REGEX);
    if (colorMatch) {
      errors.push(`${relPath}:${lineNum}: stray color class "${colorMatch[0]}" (use info-* scale or house palette instead)`);
    }

    // Check (b): native controls in .tsx files outside src/components/ui/ unless allowlisted
    if (isTsx && !isInsideUi && !isAllowlisted) {
      const inputMatch = line.match(INPUT_REGEX);
      if (inputMatch) {
        errors.push(`${relPath}:${lineNum}: unauthorized raw native control "${inputMatch[0]}" outside components/ui (use SelectionCheckbox / TactileSwitch / the ui slider)`);
      }
    }
  }
}

if (errors.length > 0) {
  for (const err of errors) {
    console.error(err);
  }
  process.exit(1);
}

console.log('ui-guard: ok');
process.exit(0);
