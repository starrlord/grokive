// Keyboard focus management for modal dialogs (a11y). Applied with `use:trapFocus`
// on a dialog panel, it:
//   1. moves focus to the first focusable control (or the panel) when the dialog opens,
//   2. cycles Tab / Shift+Tab within the dialog so focus can't escape to the page behind,
//   3. restores focus to whatever was focused before (the trigger) when the dialog closes.
// Without this, keyboard and screen-reader users tab straight into the background grid
// sitting behind the overlay. Purely additive — it changes no layout or visuals.
const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])'
].join(',');

export function trapFocus(node) {
  const previouslyFocused = document.activeElement;

  const focusable = () =>
    [...node.querySelectorAll(FOCUSABLE)].filter(
      // Skip elements that aren't actually rendered (display:none / collapsed).
      (el) => el.getClientRects().length > 0
    );

  function onKeydown(e) {
    if (e.key !== 'Tab') return;
    const items = focusable();
    if (!items.length) {
      e.preventDefault();
      node.focus?.();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    // Treat "focus on the container itself" or "focus escaped the dialog" the same
    // as the edges, so Tab/Shift+Tab from the freshly-focused panel wrap correctly.
    const atContainer = active === node || !node.contains(active);
    if (e.shiftKey) {
      if (active === first || atContainer) {
        e.preventDefault();
        last.focus();
      }
    } else if (active === last || atContainer) {
      e.preventDefault();
      first.focus();
    }
  }

  // Focus the dialog CONTAINER (not the first control). This brings focus context
  // into the dialog for keyboard/screen-reader users and lets the trap take over,
  // WITHOUT auto-focusing a leading text input — which on iOS would immediately pop
  // the on-screen keyboard every time a modal opens. Scripted focus doesn't trigger
  // :focus-visible, so no focus ring is drawn on the panel. Deferred a frame so a
  // mount transition (e.g. fly) doesn't fight it.
  const raf = requestAnimationFrame(() => node.focus?.());
  node.addEventListener('keydown', onKeydown);

  return {
    destroy() {
      cancelAnimationFrame(raf);
      node.removeEventListener('keydown', onKeydown);
      if (previouslyFocused instanceof HTMLElement && document.contains(previouslyFocused)) {
        previouslyFocused.focus();
      }
    }
  };
}
