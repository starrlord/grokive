<script>
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';

  let {
    title = 'Are you sure?',
    message = '',
    confirmLabel = 'Delete',
    danger = true,
    onconfirm = () => {},
    oncancel = () => {}
  } = $props();

  // Modal owns Escape (→ cancel); we only add Enter (→ confirm) here.
  function onkey(e) {
    if (e.key === 'Enter') onconfirm();
  }
</script>

<svelte:window onkeydown={onkey} />

<!-- z-[70]: a confirm can open on top of another dialog (e.g. the playlist editor). -->
<Modal onclose={oncancel} ariaLabel={title} z="z-[70]" panelClass="panel w-full max-w-sm rounded-2xl p-6 text-center">
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
    <Button variant="secondary" size="lg" class="flex-1" onclick={oncancel}>Cancel</Button>
    <Button variant={danger ? 'danger' : 'primary'} size="lg" class="flex-1 shadow-lg" onclick={onconfirm}>{confirmLabel}</Button>
  </div>
</Modal>
