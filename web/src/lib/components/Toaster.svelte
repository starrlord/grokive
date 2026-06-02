<script>
  import { fly, fade } from 'svelte/transition';
  import { portal } from '$lib/portal.js';
  import { toasts, dismiss } from '$lib/toast.js';
  import { selectMode } from '$lib/state.js';

  // Sit above the SelectBar (fixed at the bottom in select mode) so toasts don't
  // overlap it.
  const pad = $derived($selectMode
    ? 'calc(env(safe-area-inset-bottom) + 5rem)'
    : 'max(1rem, env(safe-area-inset-bottom))');
</script>

<!-- Portalled to <body> so it sits above all chrome regardless of stacking context.
     Container ignores pointer events; each toast re-enables them so it's tap-to-dismiss. -->
<div use:portal
     class="pointer-events-none fixed inset-x-0 bottom-0 z-[70] flex flex-col items-center gap-2 px-3 transition-[padding] duration-200"
     style="padding-bottom: {pad}">
  {#each $toasts as t (t.id)}
    <button type="button" onclick={() => dismiss(t.id)} title="Dismiss"
      class="panel pointer-events-auto flex max-w-[min(92vw,28rem)] items-center gap-2.5 rounded-full px-4 py-2.5 text-left text-sm font-semibold"
      in:fly={{ y: 24, duration: 200 }} out:fade={{ duration: 150 }}>
      {#if t.type === 'success'}
        <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 text-teal-400" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
      {:else if t.type === 'error'}
        <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 text-red-400" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
      {/if}
      <span class="min-w-0">{t.message}</span>
    </button>
  {/each}
</div>
