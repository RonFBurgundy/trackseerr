/**
 * Submits MusicBrainz release seeding fields via a hidden POST form targeting a new tab.
 * Zero `any` per TrackSeerr frontend practices.
 */
export function postSeedForm(
  actionUrl: string,
  fields: readonly (readonly [string, string])[] | readonly string[][]
): void {
  const form = document.createElement('form');
  form.method = 'post';
  form.action = actionUrl;
  form.target = '_blank';
  form.setAttribute('accept-charset', 'UTF-8');
  form.style.display = 'none';

  for (let i = 0; i < fields.length; i++) {
    const field = fields[i];
    if (!field || field.length < 2) continue;
    const name = field[0];
    const value = field[1];
    if (typeof name !== 'string' || typeof value !== 'string') continue;

    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = name;
    input.value = value;
    input.id = `mb-seed-field-${i}-${name.replace(/[^a-zA-Z0-9_-]/g, '_')}`;
    form.appendChild(input);
  }

  document.body.appendChild(form);
  try {
    form.submit();
  } finally {
    form.remove();
  }
}
