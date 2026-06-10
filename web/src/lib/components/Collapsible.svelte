<script>
  // A titled, collapsible sidebar section. The header is the toggle (chevron rotates); content
  // mounts only when open. Used to tame the filter rail — Resolution starts collapsed because
  // it's the tallest, least-used section.
  let { title, count = '', open = $bindable(true), children } = $props();
  const cid = $derived('sect-' + String(title).toLowerCase().replace(/[^a-z0-9]+/g, '-'));
</script>

<div class="mb-4">
  <button type="button" class="flex w-full items-center justify-between gap-2 py-2 text-xs font-bold uppercase tracking-wider text-muted transition hover:text-ink" aria-expanded={open} aria-controls={cid} onclick={() => (open = !open)}>
    <span class="flex items-center gap-1.5">{title}{#if count !== ''}<span class="opacity-70">{count}</span>{/if}</span>
    <svg viewBox="0 0 24 24" class="h-3.5 w-3.5 transition-transform {open ? 'rotate-90' : ''}" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
  </button>
  {#if open}
    <div id={cid} class="mt-1.5">{@render children()}</div>
  {/if}
</div>
