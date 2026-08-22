<script>
  // Floating, globally-mounted chip for the cross-library Play Queue — the playback
  // sibling of the Montage basket (MontageBasketChip). Appears whenever the queue is
  // non-empty (state.js `playQueue`), independent of select mode / current view /
  // collection, so videos gathered across libraries stay one tap from sequential
  // playback. Tapping opens a panel to review, remove, reorder-by-shuffle, clear, save
  // as a Playlist, or Play (hands the queue to the Lightbox, autoAdvance). Anchored
  // bottom-LEFT and STACKED ABOVE the Montage basket chip when that one is also present
  // (it reads `basket` purely for that layout offset); both relocate to the top on phones.
  import { fly } from 'svelte/transition';
  import {
    playQueue, basket, basketChipPos, playQueueChipPos, clearPlayQueue, togglePlayQueue, shufflePlayQueue, addPlaylist
  } from '$lib/state.js';
  import { mediaByIds } from '$lib/api.js';
  import { toast } from '$lib/toast.js';

  // onplay(startId) — resolve the queue to videos and open the Lightbox, starting at
  // startId when given (row "play from here") or the top of the queue otherwise.
  let { onplay = () => {} } = $props();

  const ids = $derived($playQueue);
  const count = $derived(ids.length);
  // Stack above the Montage basket chip only when it's actually showing AND still in
  // its default slot ($basketChipPos null — the chip is draggable), so an absent or
  // relocated basket doesn't leave this chip floating over an empty gap. Moot once
  // THIS chip has been dragged somewhere (its inline position wins anyway).
  const stacked = $derived($basket.length > 0 && !$basketChipPos && !$playQueueChipPos);

  let open = $state(false);
  let items = $state([]);
  let loading = $state(false);
  let saving = $state(false);   // Save-as-playlist name input revealed
  let name = $state('');

  // Resolve queued ids -> media (thumb + prompt) for the panel list. The ids span
  // collections you may have navigated away from, so this MUST hit the server
  // (mediaByIds), never an in-memory grid map. Video-only by construction, but we
  // still guard to videos in case a stale id resolves to something else.
  $effect(() => {
    if (!open) return;
    const want = ids;
    loading = true;
    mediaByIds(want)
      .then((list) => {
        const order = new Map(want.map((id, i) => [String(id), i]));
        items = list
          .filter((it) => it.media_type === 'video')
          .sort((a, b) => (order.get(String(a.id)) ?? 0) - (order.get(String(b.id)) ?? 0));
      })
      .catch(() => { items = []; })
      .finally(() => { loading = false; });
  });

  // Auto-close (and reset the save form) when the queue empties.
  $effect(() => { if (count === 0 && open) { open = false; saving = false; } });

  function play(startId) {
    open = false;
    onplay(startId ?? null);
  }
  function saveAsPlaylist() {
    const nm = name.trim();
    if (!nm || !count) return;
    addPlaylist(nm, [...ids]);   // playQueue is video-only, so its ids are all playable
    name = '';
    saving = false;
    toast(`Saved playlist “${nm}” (${count})`, { type: 'success' });
  }

  function onKey(e) { if (e.key === 'Escape' && open) open = false; }

  // --- Drag-to-move ----------------------------------------------------------
  // Same machinery as MontageBasketChip: the dropped spot lives in state.js
  // `playQueueChipPos` as viewport FRACTIONS (0..1 of the space the chip can
  // occupy) — persisted per-device, so it survives reloads and stays
  // proportionally placed across resizes/rotation. Null -> the CSS default
  // anchors (incl. stacking above the basket chip) apply.
  const pos = $derived($playQueueChipPos);
  let vw = $state(typeof window === 'undefined' ? 0 : window.innerWidth);
  let vh = $state(typeof window === 'undefined' ? 0 : window.innerHeight);
  let chipEl = $state(null);
  let chipW = $state(0), chipH = $state(0);
  let dragging = $state(false);
  let suppressClick = false;
  let start = null; // pointer + chip origin at pointerdown

  const placed = $derived.by(() => {
    if (!pos || !vw || !vh || !chipW || !chipH) return null;
    const x = pos.fx * Math.max(0, vw - chipW);
    const y = pos.fy * Math.max(0, vh - chipH);
    return { x, y, right: pos.fx > 0.5, up: pos.fy > 0.5 };
  });
  // Anchor the wrap by the chip corner nearest the closest viewport edge so the
  // panel grows toward open space, and cap the wrap's width so the panel can
  // never extend past the viewport (it shrinks instead — max-width: 100%).
  const wrapStyle = $derived.by(() => {
    if (!placed) return '';
    const hpos = placed.right
      ? `left:auto;right:${Math.round(vw - placed.x - chipW)}px;max-width:${Math.round(placed.x + chipW)}px;`
      : `left:${Math.round(placed.x)}px;right:auto;max-width:${Math.round(vw - placed.x - 12)}px;`;
    const vpos = placed.up
      ? `top:auto;bottom:${Math.round(vh - placed.y - chipH)}px;`
      : `top:${Math.round(placed.y)}px;bottom:auto;`;
    return hpos + vpos;
  });

  function dragStart(e) {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    const r = chipEl.getBoundingClientRect();
    start = { x: e.clientX, y: e.clientY, left: r.left, top: r.top };
    chipEl.setPointerCapture?.(e.pointerId);
  }
  function dragMove(e) {
    if (!start) return;
    const dx = e.clientX - start.x, dy = e.clientY - start.y;
    if (!dragging && Math.hypot(dx, dy) < 6) return; // still a tap until it travels
    dragging = true;
    const maxX = Math.max(0, vw - chipW), maxY = Math.max(0, vh - chipH);
    const x = Math.min(Math.max(0, start.left + dx), maxX);
    const y = Math.min(Math.max(0, start.top + dy), maxY);
    playQueueChipPos.set({ fx: maxX ? x / maxX : 0, fy: maxY ? y / maxY : 0 });
  }
  function dragEnd() {
    if (dragging) {
      // A click fires right after pointerup on a drag — it must not toggle the panel.
      suppressClick = true;
      setTimeout(() => { suppressClick = false; }, 0);
    }
    dragging = false;
    start = null;
  }
  function onChipClick() {
    if (suppressClick) return;
    open = !open;
  }
</script>

<svelte:window onkeydown={onKey} bind:innerWidth={vw} bind:innerHeight={vh} />

{#if count}
  {#if open}
    <button type="button" class="pq-scrim" aria-label="Close play queue" onclick={() => (open = false)}></button>
  {/if}
  <div class="pq-wrap" class:open class:stacked class:custom={!!placed} class:panel-down={placed && !placed.up} class:anchor-right={!!placed?.right}
    style={wrapStyle} transition:fly={{ y: 16, duration: 200 }}>
    {#if open}
      <div class="pq-panel" role="dialog" aria-label="Play queue">
        <div class="pq-head">
          <span class="pq-title">Play Queue · {count}</span>
          <button type="button" class="pq-clear" onclick={() => clearPlayQueue()}>Clear</button>
        </div>
        <div class="pq-list">
          {#if loading && !items.length}
            <p class="pq-status">Loading…</p>
          {:else if !items.length}
            <p class="pq-status">Nothing queued.</p>
          {:else}
            {#each items as it (it.id)}
              <div class="pq-row">
                <button type="button" class="pq-rowmain" title="Play from here" onclick={() => play(it.id)}>
                  {#if it.thumb}
                    <img class="pq-thumb" src={it.thumb} alt="" loading="lazy" decoding="async" />
                  {:else}
                    <span class="pq-thumb pq-thumb--empty"></span>
                  {/if}
                  <span class="pq-rowtext">{it.prompt || it.model || 'Untitled'}</span>
                </button>
                <button type="button" class="pq-remove" aria-label="Remove from queue" title="Remove" onclick={() => togglePlayQueue(it.id)}>✕</button>
              </div>
            {/each}
          {/if}
        </div>

        <div class="pq-actions">
          <button type="button" class="pq-secondary" disabled={count < 2} onclick={() => shufflePlayQueue()}
            title="Randomize the playback order">
            <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 18h1.4c1.3 0 2.5-.6 3.3-1.7l6.1-8.6c.7-1.1 2-1.7 3.3-1.7H22"/><path d="m18 2 4 4-4 4"/><path d="M2 6h1.9c1.5 0 2.9.9 3.6 2.2"/><path d="M22 18h-5.9c-1.3 0-2.6-.7-3.3-1.8l-.5-.8"/><path d="m18 14 4 4-4 4"/></svg>
            Randomize
          </button>
          <button type="button" class="pq-go" disabled={count < 1} onclick={() => play()}>
            <span aria-hidden="true">▶</span> Play{count >= 1 ? ` (${count})` : ''}
          </button>
        </div>

        {#if saving}
          <div class="pq-save">
            <input class="pq-save-input" placeholder="Playlist name" maxlength="80" bind:value={name}
              onkeydown={(e) => { if (e.key === 'Enter') saveAsPlaylist(); }} />
            <button type="button" class="pq-save-go" disabled={!name.trim()} onclick={saveAsPlaylist}>Save</button>
            <button type="button" class="pq-save-cancel" aria-label="Cancel" onclick={() => { saving = false; name = ''; }}>✕</button>
          </div>
        {:else}
          <button type="button" class="pq-saveopen" onclick={() => (saving = true)}
            title="Save this queue as a permanent Playlist">
            <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 12H3"/><path d="M16 6H3"/><path d="M12 18H3"/><path d="m16 12 5 3-5 3v-6Z"/></svg>
            Save as playlist
          </button>
        {/if}
      </div>
    {/if}

    <button type="button" bind:this={chipEl} bind:clientWidth={chipW} bind:clientHeight={chipH}
      class="pq-chip" class:grabbing={dragging} onclick={onChipClick} aria-expanded={open}
      onpointerdown={dragStart} onpointermove={dragMove} onpointerup={dragEnd} onpointercancel={dragEnd}
      title="Play queue — {count} video{count === 1 ? '' : 's'} · drag to move">
      <span class="pq-chip-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 12H3"/><path d="M16 6H3"/><path d="M12 18H3"/><path d="m16 12 5 3-5 3v-6Z"/></svg>
      </span>
      <span class="pq-chip-count">{count}</span>
      <span class="pq-chip-label">Play Queue</span>
    </button>
  </div>
{/if}

<style>
  .pq-scrim {
    background: transparent;
    inset: 0;
    position: fixed;
    z-index: 59;
  }

  .pq-wrap {
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
  /* Sit above the Montage basket chip (~2.5rem tall + 0.75rem gap) when it's present. */
  .pq-wrap.stacked { bottom: calc(max(1rem, env(safe-area-inset-bottom)) + 3.25rem); }

  .pq-chip {
    align-items: center;
    background: var(--surface-solid);
    border: 1px solid var(--line);
    border-radius: 999px;
    box-shadow: var(--shadow-dock);
    display: inline-flex;
    gap: 0.4rem;
    min-height: 2.5rem;
    padding: 0.4rem 0.85rem 0.4rem 0.55rem;
    touch-action: none; /* the chip pans itself — without this, touch drags scroll the page */
    user-select: none;
    -webkit-user-select: none;
  }
  .pq-chip.grabbing { cursor: grabbing; }
  .pq-chip:hover,
  .pq-wrap.open .pq-chip { border-color: var(--accent); }

  .pq-chip-icon { color: var(--accent); display: grid; place-items: center; }
  .pq-chip-icon svg { height: 1.15rem; width: 1.15rem; }

  .pq-chip-count {
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
  .pq-chip-label { color: var(--ink); font-size: 0.8125rem; font-weight: 800; }

  .pq-panel {
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

  .pq-head { align-items: center; display: flex; gap: 0.5rem; justify-content: space-between; }
  .pq-title { color: var(--ink); font-size: 0.8125rem; font-weight: 850; }
  .pq-clear {
    border-radius: var(--r-lg);
    color: color-mix(in srgb, var(--ink) 58%, transparent);
    font-size: 0.75rem;
    font-weight: 700;
    padding: 0.25rem 0.5rem;
  }
  .pq-clear:hover { background: color-mix(in srgb, var(--danger) 12%, transparent); color: var(--danger-ink); }

  .pq-list {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    max-height: min(46vh, 22rem);
    overflow-y: auto;
  }
  .pq-status {
    color: color-mix(in srgb, var(--ink) 58%, transparent);
    font-size: 0.8125rem;
    padding: 0.6rem;
    text-align: center;
  }

  .pq-row {
    align-items: center;
    border-radius: var(--r-lg);
    display: flex;
    gap: 0.25rem;
    padding: 0.25rem;
  }
  .pq-row:hover { background: var(--surface-2); }

  /* The thumbnail + title is itself a button: click to play from this item. */
  .pq-rowmain {
    align-items: center;
    display: flex;
    flex: 1 1 auto;
    gap: 0.5rem;
    min-width: 0;
    text-align: left;
  }

  .pq-thumb {
    background: var(--media-bg);
    border-radius: 0.4rem;
    flex: 0 0 auto;
    height: 2.4rem;
    object-fit: cover;
    width: 2.4rem;
  }
  .pq-thumb--empty { background: var(--surface-2); }

  .pq-rowtext {
    color: var(--ink);
    flex: 1 1 auto;
    font-size: 0.75rem;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .pq-remove {
    border-radius: 999px;
    color: color-mix(in srgb, var(--ink) 55%, transparent);
    display: grid;
    flex: 0 0 auto;
    height: 1.75rem;
    place-items: center;
    width: 1.75rem;
  }
  .pq-remove:hover { background: var(--surface-2); color: var(--ink); }

  .pq-actions { display: grid; gap: 0.4rem; grid-template-columns: 1fr 1fr; }

  .pq-secondary {
    align-items: center;
    background: var(--surface-2);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    color: var(--ink);
    display: inline-flex;
    font-size: 0.8125rem;
    font-weight: 800;
    gap: 0.4rem;
    justify-content: center;
    min-height: 2.4rem;
    padding: 0.5rem;
  }
  .pq-secondary:hover:not(:disabled) { border-color: var(--accent); }
  .pq-secondary:disabled { cursor: default; opacity: 0.5; }

  .pq-go {
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
  .pq-go:disabled { cursor: default; opacity: 0.5; }

  /* Save-as-playlist: a quiet full-width opener that swaps for a name input on click. */
  .pq-saveopen {
    align-items: center;
    border-radius: var(--r-lg);
    color: color-mix(in srgb, var(--ink) 62%, transparent);
    display: inline-flex;
    font-size: 0.75rem;
    font-weight: 800;
    gap: 0.4rem;
    justify-content: center;
    min-height: 2rem;
    padding: 0.3rem;
  }
  .pq-saveopen:hover { background: var(--surface-2); color: var(--ink); }

  .pq-save { align-items: center; display: flex; gap: 0.35rem; }
  .pq-save-input {
    background: color-mix(in srgb, var(--surface-2) 92%, var(--media-bg));
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    color: var(--ink);
    flex: 1 1 auto;
    font-size: 0.8125rem;
    font-weight: 600;
    min-width: 0;
    outline: none;
    padding: 0.45rem 0.6rem;
  }
  .pq-save-input:focus { border-color: var(--accent); }
  .pq-save-go {
    background: var(--accent);
    border-radius: var(--r-lg);
    color: var(--on-accent);
    flex: 0 0 auto;
    font-size: 0.8125rem;
    font-weight: 800;
    padding: 0.45rem 0.7rem;
  }
  .pq-save-go:disabled { cursor: default; opacity: 0.5; }
  .pq-save-cancel {
    border-radius: 999px;
    color: color-mix(in srgb, var(--ink) 55%, transparent);
    flex: 0 0 auto;
    height: 2rem;
    width: 2rem;
  }
  .pq-save-cancel:hover { background: var(--surface-2); color: var(--ink); }

  /* Phones / narrow viewports: the SelectBar dock fills the bottom and the montage
     chips relocate to the top — so move this chip to the top too, stacked below the
     Montage basket's slot, and flip the panel to open downward. */
  @media (max-width: 900px) {
    .pq-wrap {
      bottom: auto;
      flex-direction: column-reverse; /* chip on top, panel below */
      top: calc(56px + max(0.5rem, env(safe-area-inset-top)) + 3.25rem);
    }
    /* Below the Montage basket chip when it's showing (which itself sits below the
       status-chip slot); otherwise take the basket's slot. */
    .pq-wrap.stacked { top: calc(56px + max(0.5rem, env(safe-area-inset-top)) + 6.5rem); }
  }

  /* User-dragged position: inline left/top/right/bottom out-specificity everything;
     these two-class rules out-specificity the phone media query's flex flip so the
     panel opens away from whichever vertical half the chip sits in. */
  .pq-wrap.custom { flex-direction: column; } /* chip in bottom half -> panel above */
  .pq-wrap.custom.panel-down { flex-direction: column-reverse; } /* top half -> panel below */
  .pq-wrap.custom.anchor-right { align-items: flex-end; } /* right half -> panel grows leftward */
</style>
