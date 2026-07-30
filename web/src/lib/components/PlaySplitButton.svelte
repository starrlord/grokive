<script>
  // The accent "Play videos" control, split in two: the wide segment plays in the order
  // you're looking at (unchanged one-click behaviour — it's the most-used action, so it
  // never costs a menu hop), and the caret segment opens Play in order / Play random.
  //
  // Deliberately NOT overflow-hidden on the wrapper: Popover isn't portaled, so clipping
  // the wrapper for its rounded ends would clip the dropdown too. The two segments carry
  // rounded-l/rounded-r instead.
  import Popover from './Popover.svelte';

  let {
    label = 'Play videos',
    orderLabel = 'Play in order',
    orderHint = '',
    randomHint = 'Shuffle into a random order',
    disabled = false,
    title = undefined,
    onorder = () => {},
    onrandom = () => {}
  } = $props();
</script>

<div class="inline-flex shrink-0 items-stretch rounded-lg bg-[var(--accent)] text-[var(--on-accent)] transition {disabled ? 'opacity-50' : ''}">
  <button type="button" class="rounded-l-lg px-3 py-2 text-sm font-bold disabled:cursor-default"
    {disabled} {title} onclick={() => onorder()}>{label}</button>
  <span class="my-1.5 w-px self-stretch bg-[color-mix(in_srgb,var(--on-accent)_35%,transparent)]" aria-hidden="true"></span>
  <Popover align="right" {disabled} ariaLabel="Play options"
    triggerClass="grid w-7 place-items-center rounded-r-lg text-[var(--on-accent)] transition hover:bg-[color-mix(in_srgb,var(--on-accent)_16%,transparent)] disabled:cursor-default">
    {#snippet trigger()}
      <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
    {/snippet}
    {#snippet children(close)}
      <div class="w-[15rem] max-w-[calc(100vw-1rem)] rounded-card border border-line bg-[var(--surface-solid)] p-1.5 text-ink shadow-[0_18px_44px_-14px_rgba(0,0,0,0.6)]">
        <button type="button" class="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm font-semibold transition hover:bg-[color-mix(in_srgb,var(--accent)_14%,transparent)]"
          onclick={() => { close(); onorder(); }}>
          <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 text-muted" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="6 3 20 12 6 21 6 3"/></svg>
          <span class="min-w-0">
            <span class="block">{orderLabel}</span>
            {#if orderHint}<span class="block text-xs font-normal text-muted">{orderHint}</span>{/if}
          </span>
        </button>
        <button type="button" class="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm font-semibold transition hover:bg-[color-mix(in_srgb,var(--accent)_14%,transparent)]"
          onclick={() => { close(); onrandom(); }}>
          <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 text-muted" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 18h1.4c1.3 0 2.5-.6 3.3-1.7l6.1-8.6c.7-1.1 2-1.7 3.3-1.7H22"/><path d="m18 2 4 4-4 4"/><path d="M2 6h1.9c1.5 0 2.9.9 3.6 2.2"/><path d="M22 18h-5.9c-1.3 0-2.6-.7-3.3-1.8l-.5-.8"/><path d="m18 14 4 4-4 4"/></svg>
          <span class="min-w-0">
            <span class="block">Play random</span>
            {#if randomHint}<span class="block text-xs font-normal text-muted">{randomHint}</span>{/if}
          </span>
        </button>
      </div>
    {/snippet}
  </Popover>
</div>
