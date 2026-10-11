function legacyCopy(text: string): boolean {
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('aria-hidden', 'true');
  area.style.position = 'fixed';
  area.style.top = '0';
  area.style.left = '0';
  area.style.opacity = '0';
  document.body.appendChild(area);
  try {
    area.focus();
    area.select();
    area.setSelectionRange(0, text.length);
    return document.execCommand('copy');
  } catch (err: unknown) {
    console.warn('Legacy clipboard copy failed', err);
    return false;
  } finally {
    document.body.removeChild(area);
  }
}

/** Copy text to the clipboard; works over plain HTTP. Resolves false on failure, never throws. */
export async function copyText(text: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err: unknown) {
      console.warn('Clipboard API write failed, using fallback', err);
    }
  }
  return legacyCopy(text);
}
