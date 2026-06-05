<script>
  // Shared modal chrome: portal to <body>, blurred backdrop, click-outside +
  // Escape to close, focus trap/restore, and a consistent fade/fly transition.
  // Each dialog passes its own panel classes so layouts stay pixel-identical to
  // the hand-rolled versions this replaces; only the duplicated scaffolding moves
  // here. The panel content is the default snippet; `behind` renders inside the
  // backdrop *under* the panel (e.g. a particle layer).
  import { fly, fade } from 'svelte/transition';
  import { portal } from '$lib/portal.js';
  import { trapFocus } from '$lib/focusTrap.js';

  let {
    onclose = () => {},
    ariaLabel = undefined,
    // z-index utility class. Default sits above the app chrome; raise for dialogs
    // that can open on top of another modal (e.g. a confirm over an editor).
    z = 'z-[60]',
    overlay = 'overlay', // 'overlay' | 'overlay-strong'
    panelClass = 'panel w-full max-w-lg rounded-card',
    // Backdrop layout. Override for dialogs that must scroll on small screens.
    backdropClass = 'grid place-items-center p-4',
    closeOnOutside = true,
    closeOnEscape = true, // pass a reactive value to defer to a nested dialog
    animate = true,
    children,
    behind
  } = $props();

  const overlayClass = $derived(overlay === 'overlay-strong' ? 'bg-[var(--overlay-strong)]' : 'bg-[var(--overlay)]');

  function onWindowKey(e) {
    if (e.key === 'Escape' && closeOnEscape) onclose();
  }
</script>

<svelte:window onkeydown={onWindowKey} />

<!-- Backdrop is presentational chrome; dismissal is mirrored by Escape and the in-panel controls. -->
<div use:portal class="fixed inset-0 {z} {overlayClass} backdrop-blur-sm {backdropClass}" role="presentation"
     transition:fade={{ duration: animate ? 120 : 0 }}
     onclick={(e) => { if (closeOnOutside && e.target === e.currentTarget) onclose(); }}>
  {#if behind}{@render behind()}{/if}
  <div class={panelClass} role="dialog" aria-modal="true" aria-label={ariaLabel} tabindex="-1"
       use:trapFocus transition:fly={{ y: animate ? 18 : 0, duration: animate ? 180 : 0 }}>
    {@render children?.()}
  </div>
</div>
