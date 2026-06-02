// Copy text with a fallback. navigator.clipboard is only available in a secure
// context (HTTPS or localhost); over plain HTTP (e.g. http://<lan-ip>) it's
// undefined, so we fall back to a hidden textarea + execCommand.
export async function copyText(text) {
  const value = text || '';
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {}
  try {
    const ta = document.createElement('textarea');
    ta.value = value;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}
