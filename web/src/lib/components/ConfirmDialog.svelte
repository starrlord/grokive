<script>
  import { fly, fade } from 'svelte/transition';
  import { portal } from '$lib/portal.js';

  let {
    title = 'Are you sure?',
    message = '',
    confirmLabel = 'Delete',
    danger = true,
    onconfirm = () => {},
    oncancel = () => {}
  } = $props();

  function onkey(e) {
    if (e.key === 'Escape') oncancel();
    else if (e.key === 'Enter') onconfirm();
  }
</script>

<svelte:window on:keydown={onkey} />

<!-- Backdrop is presentational chrome; dismissal is mirrored by Escape (window handler) and the buttons. -->
<div use:portal class="fixed inset-0 z-[70] grid place-items-center bg-[var(--overlay)] p-4 backdrop-blur-sm" role="presentation"
     transition:fade={{ duration: 120 }} onclick={(e) => { if (e.target === e.currentTarget) oncancel(); }}>
  <div class="panel w-full max-w-sm rounded-2xl p-6 text-center" role="dialog" aria-modal="true" aria-label={title} tabindex="-1" transition:fly={{ y: 16, duration: 160 }}>
    <div class="mx-auto mb-4 grid h-14 w-14 place-items-center rounded-full {danger ? 'bg-[var(--danger-bg-strong)] text-[var(--danger-ink)]' : 'text-[var(--accent)]'}"
         style={danger ? '' : 'background: color-mix(in srgb, var(--accent) 15%, transparent)'}>
      {#if danger}
        <svg viewBox="0 0 24 24" class="h-7 w-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
      {:else}
        <svg viewBox="0 0 24 24" class="h-7 w-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>
      {/if}
    </div>
    <h2 class="mb-1 text-lg font-bold">{title}</h2>
    {#if message}<p class="mb-5 text-sm leading-relaxed text-muted">{message}</p>{/if}
    <div class="flex gap-2">
      <button class="flex-1 rounded-lg border border-line py-2.5 font-semibold transition hover:bg-[var(--surface-2)]" onclick={oncancel}>Cancel</button>
      <button class="flex-1 rounded-lg py-2.5 font-bold text-[var(--on-accent)] shadow-lg transition {danger ? 'bg-[var(--danger)] hover:bg-[var(--danger-hover)]' : 'bg-[var(--accent)] hover:opacity-90'}" onclick={onconfirm}>{confirmLabel}</button>
    </div>
  </div>
</div>
