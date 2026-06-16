<script>
  // Floating, globally-mounted chip for the cross-library Montage queue ("basket").
  // Appears whenever the basket is non-empty (state.js `basket`), independent of
  // select mode / current view / collection — so clips gathered across libraries
  // stay one tap from a montage. Tapping opens a panel to review, remove, clear, or
  // fire the montage. Anchored bottom-LEFT so it never collides with the bottom-RIGHT
  // MontageStatusChip on desktop; both relocate to the top on phones (see media query).
  import { fly } from 'svelte/transition';
  import { basket, clearBasket, toggleBasket } from '$lib/state.js';
  import { mediaByIds } from '$lib/api.js';

  let { onmontage = () => {} } = $props();

  const ids = $derived($basket);
  const count = $derived(ids.length);
  let open = $state(false);
  let items = $state([]);
  let loading = $state(false);

  // Resolve queued ids -> media (thumb + prompt) for the panel list. The ids span
  // collections you may have navigated away from, so this MUST hit the server
  // (mediaByIds), never an in-memory grid map — that's the whole point of the queue.
  // Re-runs whenever the panel is open and the basket changes.
  $effect(() => {
    if (!open) return;
    const want = ids;
    loading = true;
    mediaByIds(want)
      .then((list) => {
        const order = new Map(want.map((id, i) => [String(id), i]));
        items = list.slice().sort((a, b) => (order.get(String(a.id)) ?? 0) - (order.get(String(b.id)) ?? 0));
      })
      .catch(() => { items = []; })
      .finally(() => { loading = false; });
  });

  // Auto-close when the queue empties (last item removed from the panel or elsewhere).
  $effect(() => { if (count === 0 && open) open = false; });

  function onKey(e) { if (e.key === 'Escape' && open) open = false; }
</script>

<svelte:window onkeydown={onKey} />

{#if count}
  {#if open}
    <button type="button" class="basket-scrim" aria-label="Close montage queue" onclick={() => (open = false)}></button>
  {/if}
  <div class="basket-wrap" class:open transition:fly={{ y: 16, duration: 200 }}>
    {#if open}
      <div class="basket-panel" role="dialog" aria-label="Montage queue">
        <div class="basket-head">
          <span class="basket-title">Montage queue · {count}</span>
          <button type="button" class="basket-clear" onclick={() => clearBasket()}>Clear</button>
        </div>
        <div class="basket-list">
          {#if loading && !items.length}
            <p class="basket-status">Loading…</p>
          {:else if !items.length}
            <p class="basket-status">Nothing queued.</p>
          {:else}
            {#each items as it (it.id)}
              <div class="basket-row">
                {#if it.thumb}
                  <img class="basket-thumb" src={it.thumb} alt="" loading="lazy" decoding="async" />
                {:else}
                  <span class="basket-thumb basket-thumb--empty"></span>
                {/if}
                <span class="basket-rowtext">{it.prompt || it.model || 'Untitled'}</span>
                <button type="button" class="basket-remove" aria-label="Remove from queue" title="Remove" onclick={() => toggleBasket(it.id)}>✕</button>
              </div>
            {/each}
          {/if}
        </div>
        <button type="button" class="basket-go" disabled={count < 2} onclick={() => { open = false; onmontage(); }}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
          Make Montage{count >= 2 ? ` (${count})` : ''}
        </button>
        {#if count < 2}<p class="basket-hint">Add at least 2 videos to montage.</p>{/if}
      </div>
    {/if}

    <button type="button" class="basket-chip" onclick={() => (open = !open)} aria-expanded={open}
      title="Montage queue — {count} video{count === 1 ? '' : 's'}">
      <span class="basket-chip-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
      </span>
      <span class="basket-chip-count">{count}</span>
      <span class="basket-chip-label">queued</span>
    </button>
  </div>
{/if}

<style>
  .basket-scrim {
    background: transparent;
    inset: 0;
    position: fixed;
    z-index: 59;
  }

  .basket-wrap {
    align-items: flex-start;
    bottom: max(1rem, env(safe-area-inset-bottom));
    display: flex;
    flex-direction: column; /* panel above the chip on desktop */
    gap: 0.5rem;
    left: max(1rem, env(safe-area-inset-left));
    max-width: min(22rem, calc(100vw - 2rem));
    position: fixed;
    z-index: 60;
  }

  .basket-chip {
    align-items: center;
    background: var(--surface-solid);
    border: 1px solid var(--line);
    border-radius: 999px;
    box-shadow: var(--shadow-dock);
    display: inline-flex;
    gap: 0.4rem;
    min-height: 2.5rem;
    padding: 0.4rem 0.85rem 0.4rem 0.55rem;
  }
  .basket-chip:hover,
  .basket-wrap.open .basket-chip { border-color: var(--accent); }

  .basket-chip-icon { color: var(--accent); display: grid; place-items: center; }
  .basket-chip-icon svg { height: 1.15rem; width: 1.15rem; }

  .basket-chip-count {
    background: var(--accent);
    border-radius: 999px;
    color: var(--on-accent);
    display: grid;
    font-size: 0.75rem;
    font-weight: 900;
    height: 1.4rem;
    min-width: 1.4rem;
    padding: 0 0.4rem;
    place-items: center;
  }
  .basket-chip-label { color: var(--ink); font-size: 0.8125rem; font-weight: 800; }

  .basket-panel {
    background: var(--surface-solid);
    border: 1px solid var(--line);
    border-radius: var(--r-xl);
    box-shadow: var(--shadow-dock);
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    padding: 0.6rem;
    width: min(20rem, calc(100vw - 2rem));
  }

  .basket-head { align-items: center; display: flex; gap: 0.5rem; justify-content: space-between; }
  .basket-title { color: var(--ink); font-size: 0.8125rem; font-weight: 850; }
  .basket-clear {
    border-radius: var(--r-lg);
    color: color-mix(in srgb, var(--ink) 58%, transparent);
    font-size: 0.75rem;
    font-weight: 700;
    padding: 0.25rem 0.5rem;
  }
  .basket-clear:hover { background: color-mix(in srgb, var(--danger) 12%, transparent); color: var(--danger-ink); }

  .basket-list {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    max-height: min(46vh, 22rem);
    overflow-y: auto;
  }
  .basket-status {
    color: color-mix(in srgb, var(--ink) 58%, transparent);
    font-size: 0.8125rem;
    padding: 0.6rem;
    text-align: center;
  }

  .basket-row {
    align-items: center;
    border-radius: var(--r-lg);
    display: flex;
    gap: 0.5rem;
    padding: 0.25rem;
  }
  .basket-row:hover { background: var(--surface-2); }

  .basket-thumb {
    background: var(--media-bg);
    border-radius: 0.4rem;
    flex: 0 0 auto;
    height: 2.4rem;
    object-fit: cover;
    width: 2.4rem;
  }
  .basket-thumb--empty { background: var(--surface-2); }

  .basket-rowtext {
    color: var(--ink);
    flex: 1 1 auto;
    font-size: 0.75rem;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .basket-remove {
    border-radius: 999px;
    color: color-mix(in srgb, var(--ink) 55%, transparent);
    display: grid;
    flex: 0 0 auto;
    height: 1.75rem;
    place-items: center;
    width: 1.75rem;
  }
  .basket-remove:hover { background: var(--surface-2); color: var(--ink); }

  .basket-go {
    align-items: center;
    background: var(--accent);
    border-radius: var(--r-lg);
    color: var(--on-accent);
    display: inline-flex;
    font-size: 0.8125rem;
    font-weight: 800;
    gap: 0.45rem;
    justify-content: center;
    min-height: 2.4rem;
    padding: 0.5rem;
  }
  .basket-go:disabled { cursor: default; opacity: 0.5; }

  .basket-hint {
    color: color-mix(in srgb, var(--ink) 55%, transparent);
    font-size: 0.6875rem;
    text-align: center;
  }

  /* Phones / narrow viewports: the SelectBar dock wraps and fills the bottom and the
     MontageStatusChip relocates to the top — so move this chip to the top too, stacked
     just BELOW the status chip's slot, and flip the panel to open downward. */
  @media (max-width: 900px) {
    .basket-wrap {
      bottom: auto;
      flex-direction: column-reverse; /* chip on top, panel below */
      top: calc(56px + max(0.5rem, env(safe-area-inset-top)) + 3.25rem);
    }
  }
</style>
